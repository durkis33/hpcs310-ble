#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["bleak>=0.22", "matplotlib>=3.7", "numpy>=1.24", "colour-science>=0.4.7"]
# ///
"""
hpcs310.py - Read spectrum from a HopooColor HPCS-310 / HPCS-330 spectrometer
             over Bluetooth Low Energy from a PC.

Protocol reverse-engineered from the official "虹谱光色" (HopooColor) Android app
(WeChat mini-program packaged as a standalone app, AppID wx5d7ad680ce36ae10).

The device is a BLE UART bridge (HM-10 / TI CC254x class):
    Service        UUID  0000FFE0-0000-1000-8000-00805F9B34FB
    Characteristic UUID  0000FFE1-0000-1000-8000-00805F9B34FB   (write + notify)

All frames: host writes  0x8C <cmd> [args...]   with Write-Without-Response.
            device notifies 0x8C <cmd> <data...> back on the same characteristic.

Measurement handshake (matches app pages/home/index state machine):
    0x8C 05          read integration time            -> 0x8C 05 <u32 us LE at [2:6]>
    0x8C 0E 01       start one sample (0E 02 = Lx var) -> 0x8C 0E ...  (ack)
    0x8C 03          poll sample state                -> 0x8C 03 ... ; byte[3]==1 => ready
    0x8C 13 31       read result (streamed, multi-pkt) -> 0x8C 13 <u16 len BE> <payload...>
    0x8C 25          stop / abort
    0x8C ED          heartbeat
    0x8C EE          device info (name/sn/ver/battery)
    0x8C C3          battery/status

Result payload layout (parsed by the app's analysisSepc):
    [0]      = 0x8C
    [1]      = 0x13
    [2:4]    = u16 BIG-endian payload length; total frame W = length + 4 (e.g. 0x0B70+4=2932)
    [40:...] = float32 LE metrics block (fields depend on device model)
    last 8   = float32 LE StartWave , float32 LE EndWave  (nm)
    [W-2692 : W-8] = 671 x float32 LE spectral irradiance  (uW/cm^2/nm)
                     spectrum[i] corresponds to wavelength StartWave + i (1 nm step);
                     only the first round(EndWave-StartWave)+1 samples are valid.

Usage:
    python3 hpcs310.py scan
    python3 hpcs310.py measure [--address AA:BB:..] [--out DIR] [--lx] [--no-plot]
        # dumps a folder (default device_SN_timestamp/) with:
        #   spectrum.csv  measurement.json  frame.bin  spectrum.png
    python3 hpcs310.py decode  result.bin [--csv out.csv]     # offline packet decode
    python3 hpcs310.py selftest                               # parser self-check, no HW

Live BLE requires a Bluetooth adapter reachable in the current network namespace.
Inside a Docker container that means running the container with host networking, e.g.
    docker run --net=host --privileged -v /var/run/dbus:/var/run/dbus ...
because Linux AF_BLUETOOTH sockets are not network-namespace aware.
"""

import argparse
import asyncio
import json
import os
import struct
import sys
import time
import math
import statistics

__version__ = "0.2.1"
VERBOSE = False


def vlog(*a):
    if VERBOSE:
        print("[v]", *a, file=sys.stderr)


def _hex(b):
    return bytes(b).hex(" ")

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHAR_UUID    = "0000ffe1-0000-1000-8000-00805f9b34fb"

NAME_PREFIX  = "HPCS"

# --- command frames -------------------------------------------------------
CMD_INFO   = bytes([0x8C, 0xEE])
CMD_BATT   = bytes([0x8C, 0xC3])
CMD_INTEG  = bytes([0x8C, 0x05])
CMD_START  = bytes([0x8C, 0x0E, 0x01])
CMD_START_LX = bytes([0x8C, 0x0E, 0x02])
CMD_STATE  = bytes([0x8C, 0x03])
CMD_RESULT = bytes([0x8C, 0x13, 0x31])
CMD_STOP   = bytes([0x8C, 0x25])
CMD_HEART  = bytes([0x8C, 0xED])
CMD_FLICK_SPEED = bytes([0x8C, 0x3D])
CMD_FLICK_GEAR_AUTO = bytes([0x8C, 0x37, 0x01])
CMD_FLICK_GEAR_MODE = bytes([0x8C, 0x38])
CMD_FLICK_GEAR = bytes([0x8C, 0x36])
CMD_FLICK_RATE_AUTO = bytes([0x8C, 0x41, 0x01])
CMD_FLICK_RATE_MODE = bytes([0x8C, 0x3F])
CMD_FLICK_START = bytes([0x8C, 0x0E, 0x03])
CMD_FLICK_START_CONTINUOUS = bytes([0x8C, 0x0E, 0x04])
CMD_FLICK_STATE = bytes([0x8C, 0x3B])
CMD_FLICK_PARAMS = bytes([0x8C, 0x3C])
CMD_FLICK_WAVE = bytes([0x8C, 0x3A])
FLICK_WINDOW_MS = [100000, 50000, 20000, 10000, 5000, 2000, 1000, 500, 200, 100, 50]
# APK windows imply 10,000 internal samples; that record is not exposed by 3A.
FLICK_SAMPLE_RATES_HZ = [100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000]

N_SPECTRUM = 671          # fixed spectral buffer length (float32 each)
SPEC_BYTES = N_SPECTRUM * 4       # 2684
TRAILER    = 8                    # StartWave + EndWave floats
RESULT_TAIL = SPEC_BYTES + TRAILER  # 2692

# device-type codes, from app getTypeByName()
def type_by_name(name: str) -> int:
    t = (name or "").upper()
    def has(*xs): return any(x in t for x in xs)
    if has("330UV", "310UV"): return 2
    if has("330IR", "310IR"): return 3
    if has("330PR", "310PR"): return 4
    if has("330C",  "310C"):  return 5
    if has("330P",  "310P"):  return 1
    if has("330", "310"):     return 0
    return 0


# ----------------------------------------------------------------------------
# Result decoding
# ----------------------------------------------------------------------------
def _f32(buf, off):
    return struct.unpack_from("<f", buf, off)[0]

# Ordered metric field names per device type (offset 40, sequential float32 LE).
# Verbatim from the app's analysisSepc. Lists marked (partial) are truncated in
# the minified source tail; spectrum extraction below is independent of these.
_METRICS = {
    2: ["fESuv","fEuvc","fEuvb","fEuva","fEuv","fEb","fEg","F",
        "UV_E","UVA_E","UVB_E","UVC_E","HlafWidth","PeakWave","CentreWave",
        "CentroidWave","IntegTime0","VPeak","VDark","VDarkDAC"],
    3: ["fEir","fEr","F","HlafWidth","PeakWave","CentreWave","CentroidWave",
        "fFreq","fPF","fPI","fDC","fFC","IntegTime0","VPeak","VDark","VDarkDAC"],
    5: ["fC_LUX","fCPFD","fCPFD_UV","fCPFD_B","fCPFD_G","fCPFD_R","fLx","fEfc",
        "cct","duv","x","y","u","v","u2","v2","fSDCM","Ra",
        "R1","R2","R3","R4","R5","R6","R7","R8","R9","R10","R11","R12","R13","R14","R15",
        "F","fSP","DominantWave","Purity","HlafWidth","PeakWave","CentreWave",
        "CentroidWave","fRraito","fGraito","fBraito","fFreq","fPF","fPI","fDC","fFC",
        "IntegTime0","VPeak","VDark","VDarkDAC"],
    1: ["fPAR","fPPFD","fPPFD_UV","fPPFD_B","fPPFD_G","fPPFD_R","fPPFD_FR",
        "fPPFD_IR","fKppfv","fPPFD_RB","fYPFD","fEch_A","fEch_B","fDLI","fCLI",
        "fLx","fEfc","cct","duv","x","y","u","v","u2","v2","fSDCM","Ra"]
        + [f"R{i}" for i in range(1, 16)]
        + ["F","fSP","DominantWave","Purity","HlafWidth","PeakWave","CentreWave",
           "CentroidWave","fRraito","fGraito","fBraito","fFreq","fPF","fPI","fDC","fFC",
           "IntegTime0","VPeak","VDark","VDarkDAC"],
}
# default (type 0 and unknown) layout. The app's analysisSepc `else` branch adds
# four fields only when the firmware version > 2005, then the trailing signal
# fields (IntegTime0, VPeak, VDark, VDarkDAC) always follow.
_METRICS_DEFAULT_HEAD = (
    ["fLx","fEfc","cct","duv","x","y","u","v","u2","v2","fSDCM","Ra"]
    + [f"R{i}" for i in range(1, 16)]
    + ["fSP","DominantWave","Purity","HlafWidth","PeakWave","CentreWave",
       "CentroidWave","fRraito","fGraito","fBraito"]
)
_METRICS_DEFAULT_TAIL = ["IntegTime0", "VPeak", "VDark", "VDarkDAC"]


def _default_metric_names(iVer):
    extras = ["fEML", "fEeml", "fEmlRatio", "fEDI_lx"] if iVer and iVer > 2005 else []
    return _METRICS_DEFAULT_HEAD + extras + _METRICS_DEFAULT_TAIL


def signal_pct(v):
    """Peak/Dark signal percentage, verbatim from the app's GetPercent()."""
    try:
        return round(float(v) / 64500 * 100)
    except (TypeError, ValueError):
        return None


def decode_result(buf: bytes, dev_type: int = 0, iVer: int = 0):
    """Decode a full 0x8C 0x13 result frame. Returns dict with spectrum + metrics."""
    buf = bytes(buf)
    if len(buf) < 4 or buf[0] != 0x8C or buf[1] != 0x13:
        raise ValueError("not a 0x8C13 result frame")
    # header u16 (big-endian) at [2:4] is the payload length; total frame = len + 4
    # (verified against the app's exported CSV: the frame ends with StartWave then
    #  EndWave floats at W-8 / W-4, and the final 4-byte BLE fragment carries EndWave)
    W = struct.unpack_from(">H", buf, 2)[0] + 4
    if len(buf) < W:
        raise ValueError(f"frame truncated: have {len(buf)}, need {W}")
    if W < 40 + RESULT_TAIL:
        raise ValueError(f"frame too short for spectrum: W={W}")

    start_wave = _f32(buf, W - 8)
    end_wave   = _f32(buf, W - 4)
    spec_off   = W - RESULT_TAIL          # == W - 2692
    spectrum   = list(struct.unpack_from("<%df" % N_SPECTRUM, buf, spec_off))

    n_valid = int(round(end_wave - start_wave)) + 1
    n_valid = max(0, min(n_valid, N_SPECTRUM))
    wavelengths = [start_wave + i for i in range(n_valid)]
    intensities = spectrum[:n_valid]

    # best-effort named metrics (offset 40, sequential float32 LE)
    names = _METRICS.get(dev_type, _default_metric_names(iVer))
    metrics = {}
    for i, nm in enumerate(names):
        off = 40 + 4 * i
        if off + 4 <= spec_off:
            metrics[nm] = round(_f32(buf, off), 5)

    return {
        "device_type": dev_type,
        "start_wave": start_wave,
        "end_wave": end_wave,
        "n_valid": n_valid,
        "wavelengths": wavelengths,
        "intensities": intensities,
        "spectrum_raw": spectrum,
        "metrics": metrics,
        "frame_len": W,
    }


def decode_info(buf: bytes):
    """Decode the 0x8C 0xEE device-info notification (see ConnectCom type-4 handler)."""
    a = bytes(buf)
    name = "".join(chr(c) for c in a[2:12] if c != 0)
    sn   = struct.unpack_from("<I", a, 12)[0] if len(a) >= 16 else None
    ver  = struct.unpack_from("<H", a, 16)[0] if len(a) >= 18 else None
    batt = a[18] if len(a) >= 19 else None
    status = a[19] if len(a) >= 20 else None
    return {"name": name, "sn": sn, "iVer": ver, "battery": batt,
            "status": status, "type": type_by_name(name)}


def write_csv(path, res):
    with open(path, "w") as f:
        f.write("wavelength_nm,intensity_uW_cm2_nm\n")
        for wl, iv in zip(res["wavelengths"], res["intensities"]):
            f.write(f"{wl:.1f},{iv:.6g}\n")


def print_summary(res, info=None):
    if info:
        print(f"device : {info['name']}  sn={info['sn']}  fw={info['iVer']} "
              f"batt={info['battery']}%  type={info['type']}")
    print(f"range  : {res['start_wave']:.1f} - {res['end_wave']:.1f} nm "
          f"({res['n_valid']} points, 1 nm step)")
    m = res["metrics"]
    show = [k for k in ("fLx", "cct", "duv", "x", "y", "Ra", "PeakWave",
                        "fPAR", "fPPFD", "fEir", "fESuv") if k in m]
    if show:
        print("metrics:", "  ".join(f"{k}={m[k]}" for k in show))
    if res["intensities"]:
        peak_i = max(range(res["n_valid"]), key=lambda i: res["intensities"][i])
        print(f"peak   : {res['wavelengths'][peak_i]:.1f} nm "
              f"= {res['intensities'][peak_i]:.4g} uW/cm^2/nm")
    if VERBOSE and m:
        print(f"all metrics (type {res['device_type']}):")
        for k, v in m.items():
            print(f"    {k:12s} = {v}")


def _sanitize(name):
    out = "".join("-" if c in " /\\\t" else c for c in (name or "").strip())
    return out or "HPCS"


def build_measurement(info, res, integ_ms, address):
    """Assemble the full per-measurement record dict for measurement.json."""
    m = res["metrics"]
    peak_i = (max(range(res["n_valid"]), key=lambda i: res["intensities"][i])
              if res["n_valid"] else None)
    vpeak = m.get("VPeak")
    vdark = m.get("VDark")
    signal = {}
    if vpeak is not None:
        signal["VPeak"] = vpeak
        signal["peak_pct"] = signal_pct(vpeak)
    if vdark is not None:
        signal["VDark"] = vdark
        signal["dark_pct"] = signal_pct(vdark)
    return {
        "device": {
            "name": info.get("name"),
            "sn": info.get("sn"),
            "firmware": info.get("iVer"),
            "battery": info.get("battery"),
            "status": info.get("status"),
            "type": info.get("type"),
            "address": address,
        },
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "measurement": {
            "integration_ms": integ_ms,
            "start_wave": res["start_wave"],
            "end_wave": res["end_wave"],
            "n_points": res["n_valid"],
            "peak_wavelength": res["wavelengths"][peak_i] if peak_i is not None else None,
            "peak_value": res["intensities"][peak_i] if peak_i is not None else None,
        },
        "signal": signal,
        "metrics": m,
        "spectrum": {
            "wavelength_nm": [round(wl, 1) for wl in res["wavelengths"]],
            "intensity_uW_cm2_nm": [round(iv, 6) for iv in res["intensities"]],
        },
    }


def write_outputs(folder, info, res, frame, integ_ms, address, make_plot=True):
    """Write spectrum.csv, measurement.json, frame.bin (+ spectrum.png) into `folder`.
    Returns the list of files written. Pure: no BLE, safe to unit-test."""
    os.makedirs(folder, exist_ok=True)
    written = []

    raw_path = os.path.join(folder, "frame.bin")
    with open(raw_path, "wb") as f:
        f.write(frame)
    written.append(raw_path)

    csv_path = os.path.join(folder, "spectrum.csv")
    write_csv(csv_path, res)
    written.append(csv_path)

    meta = build_measurement(info, res, integ_ms, address)
    meta_path = os.path.join(folder, "measurement.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    written.append(meta_path)

    if make_plot:
        png_path = os.path.join(folder, "spectrum.png")
        try:
            vpeak = res["metrics"].get("VPeak")
            vdark = res["metrics"].get("VDark")
            plot_spectrum(
                res["wavelengths"], res["intensities"], png_path,
                integration_ms=integ_ms,
                peak_count=int(vpeak) if vpeak is not None else None,
                peak_pct=signal_pct(vpeak) if vpeak is not None else None,
                dark_count=int(vdark) if vdark is not None else None,
                dark_pct=signal_pct(vdark) if vdark is not None else None,
            )
            written.append(png_path)
        except Exception as e:  # matplotlib missing or plot failure: skip PNG only
            print(f"warning: could not render spectrum.png ({e})", file=sys.stderr)

    return written




# ----------------------------------------------------------------------------
# Standards-based derived light-quality metrics
# ----------------------------------------------------------------------------
def derive_quality_metrics(wavelengths, intensities):
    """Calculate versioned quality metrics from the immutable measured SPD.

    These values are software-derived, never instrument-reported. SSI is not
    calculated here because it requires an explicit reference spectrum.
    """
    try:
        import colour
        from colour import SpectralDistribution
        from colour.quality import colour_fidelity_index_ANSIIESTM3018
    except ImportError as e:
        raise RuntimeError(
            "derived metrics require colour-science (pip install colour-science)"
        ) from e

    data = {float(w): max(0.0, float(v))
            for w, v in zip(wavelengths, intensities)
            if 380.0 <= float(w) <= 780.0}
    if len(data) < 2:
        raise ValueError("SPD does not contain enough data in 380-780 nm")

    sd = SpectralDistribution(data, name="HPCS measured SPD")
    tm30 = colour_fidelity_index_ANSIIESTM3018(sd, additional_data=True)
    cqs = colour.colour_quality_scale(sd, method="NIST CQS 9.0")
    tlci = colour.television_lighting_consistency_index(sd)

    return {
        "provenance": {
            "source": "software-derived from stored HPCS SPD",
            "library": "colour-science",
            "library_version": getattr(colour, "__version__", None),
            "input_range_nm": [min(data), max(data)],
            "input_spacing_nm": 1.0,
            "negative_values_clamped_to_zero": True,
        },
        "tm30_18": {
            "Rf": float(tm30.R_f),
            "Rg": float(tm30.R_g),
            "CCT": float(tm30.CCT),
            "Duv": float(tm30.D_uv),
            "Rf_hue_bins": [float(x) for x in tm30.R_fs],
            "chroma_shift_hue_bins": [float(x) for x in tm30.R_cs],
            "hue_shift_hue_bins": [float(x) for x in tm30.R_hs],
        },
        "cqs_nist_9_0": float(cqs),
        "tlci_2012": float(tlci),
        "ssi": None,
        "ssi_note": "Requires an explicit reference spectrum; use --ssi-reference.",
    }


def derive_ssi(wavelengths, intensities, reference_name):
    """Calculate Academy SSI against an explicitly named built-in reference."""
    try:
        import colour
        from colour import SpectralDistribution
    except ImportError as e:
        raise RuntimeError(
            "SSI requires colour-science (pip install colour-science)"
        ) from e
    data = {float(w): max(0.0, float(v))
            for w, v in zip(wavelengths, intensities)
            if 380.0 <= float(w) <= 780.0}
    sd = SpectralDistribution(data, name="HPCS measured SPD")
    if reference_name not in colour.SDS_ILLUMINANTS:
        raise ValueError(
            f"unknown SSI reference {reference_name!r}; choose a Colour SDS_ILLUMINANTS key")
    ref = colour.SDS_ILLUMINANTS[reference_name]
    return {
        "value": float(colour.spectral_similarity_index(sd, ref)),
        "reference": reference_name,
        "library": "colour-science",
        "library_version": getattr(colour, "__version__", None),
    }


def load_measurement_spd(path):
    with open(path, "r") as f:
        record = json.load(f)
    spectrum = record.get("spectrum") or {}
    wl = spectrum.get("wavelength_nm")
    iv = spectrum.get("intensity_uW_cm2_nm")
    if not wl or not iv or len(wl) != len(iv):
        raise ValueError("measurement JSON has no valid stored SPD")
    return record, wl, iv


def write_derived_metrics(measurement_path, out_path=None, ssi_reference=None):
    record, wl, iv = load_measurement_spd(measurement_path)
    derived = derive_quality_metrics(wl, iv)
    if ssi_reference:
        derived["ssi"] = derive_ssi(wl, iv, ssi_reference)
        derived.pop("ssi_note", None)
    payload = {
        "source_measurement": os.path.basename(measurement_path),
        "source_device": record.get("device"),
        "source_captured_at": record.get("captured_at"),
        "derived_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "derived_metrics": derived,
    }
    out_path = out_path or os.path.join(
        os.path.dirname(measurement_path), "derived_metrics.json")
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    return out_path, payload


# ----------------------------------------------------------------------------
# Plotting (styled like the app's "Spec." chart)
# ----------------------------------------------------------------------------
def wavelength_to_rgb(nm, gamma=0.8):
    """Approximate visible-wavelength -> RGB (Dan Bruton's algorithm).
    Returns (r,g,b) in 0..1. Fades to black outside ~380..780 nm."""
    w = float(nm)
    if 380 <= w < 440:
        r, g, b = -(w - 440) / (440 - 380), 0.0, 1.0
    elif 440 <= w < 490:
        r, g, b = 0.0, (w - 440) / (490 - 440), 1.0
    elif 490 <= w < 510:
        r, g, b = 0.0, 1.0, -(w - 510) / (510 - 490)
    elif 510 <= w < 580:
        r, g, b = (w - 510) / (580 - 510), 1.0, 0.0
    elif 580 <= w < 645:
        r, g, b = 1.0, -(w - 645) / (645 - 580), 0.0
    elif 645 <= w <= 780:
        r, g, b = 1.0, 0.0, 0.0
    else:
        r, g, b = 0.0, 0.0, 0.0
    # intensity falloff near the limits of vision
    if 380 <= w < 420:
        f = 0.3 + 0.7 * (w - 380) / (420 - 380)
    elif 420 <= w < 701:
        f = 1.0
    elif 701 <= w <= 780:
        f = 0.3 + 0.7 * (780 - w) / (780 - 700)
    else:
        f = 0.0
    return ((r * f) ** gamma, (g * f) ** gamma, (b * f) ** gamma)


def plot_spectrum(wavelengths, intensities, out_path, integration_ms=None,
                  peak_count=None, peak_pct=None, dark_count=None,
                  dark_pct=None, title=None):
    """Render the spectrum as a PNG styled like the app's "Spec." chart:
    peak-normalised, area filled with true spectral colours, red peak marker,
    380..780 nm axis. Imports matplotlib lazily; caller handles ImportError."""
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection

    wl = np.asarray(wavelengths, dtype=float)
    iv = np.asarray(intensities, dtype=float)
    if wl.size == 0:
        raise ValueError("no data points to plot")

    peak = iv.max()
    y = iv / peak if peak > 0 else iv
    peak_wl = float(wl[int(np.argmax(iv))])

    fig, ax = plt.subplots(figsize=(10, 5.6), dpi=140)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    # spectral-coloured fill: one quad per 1 nm segment, clipped to the curve
    verts, colors = [], []
    for i in range(len(wl) - 1):
        x0, x1 = wl[i], wl[i + 1]
        y0, y1 = y[i], y[i + 1]
        verts.append([(x0, 0), (x0, y0), (x1, y1), (x1, 0)])
        colors.append(wavelength_to_rgb(0.5 * (x0 + x1)))
    quads = PolyCollection(verts, facecolors=colors, edgecolors="none",
                           antialiaseds=True)
    ax.add_collection(quads)

    # subtle darker top outline, like the app's crisp curve edge
    ax.plot(wl, y, color=(0.25, 0.25, 0.25), lw=0.6, alpha=0.35)

    # red peak marker
    ax.axvline(peak_wl, color="#e02020", lw=1.6)

    # axes limits / ticks matching the app
    ax.set_xlim(380, 780)
    ax.set_ylim(0, 1.02)
    ax.set_xticks(np.linspace(380, 780, 13).round().astype(int))
    ax.set_yticks([0, 0.2, 0.4, 0.6, 0.8, 1])
    ax.grid(True, color="#e6e6ee", lw=0.8)
    ax.set_axisbelow(False)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#c8c8d0")
    ax.tick_params(colors="#606060", labelsize=10)

    # title + subtitle in the app's wording
    peak_val = float(iv[int(np.argmax(iv))])
    head = title or (f"Wavelength:{peak_wl:.0f}nm  "
                     f"Spectral:{peak_val:.3f}uw/cm²/nm")
    ax.set_title(head, color="#606060", fontsize=15, pad=26)

    sub = []
    if integration_ms is not None:
        sub.append(f"Integration Time:{integration_ms:g}ms")
    if peak_count is not None:
        sub.append(f"Peak:{peak_count}"
                   + (f"({peak_pct:g}%)" if peak_pct is not None else ""))
    if dark_count is not None:
        sub.append(f"Dark:{dark_count}"
                   + (f"({dark_pct:g}%)" if dark_pct is not None else ""))
    if sub:
        ax.annotate("        ".join(sub), xy=(0.5, 1.015),
                    xycoords="axes fraction", ha="center", va="bottom",
                    color="#707070", fontsize=10)

    fig.tight_layout()
    fig.savefig(out_path, facecolor="white")
    plt.close(fig)


# ----------------------------------------------------------------------------
# Live BLE
# ----------------------------------------------------------------------------
async def _scan(timeout=8.0):
    from bleak import BleakScanner
    print(f"scanning {timeout:.0f}s for {NAME_PREFIX}* ...")
    devs = await BleakScanner.discover(timeout=timeout)
    hits = [d for d in devs if (d.name or "").startswith(NAME_PREFIX)]
    for d in hits:
        print(f"  {d.address}  {d.name}")
    if not hits:
        print("  (none found)")
    return hits


class _Rx:
    """Collects notification chunks, exposes await_frame helpers."""
    def __init__(self):
        self.chunks = []
        self.ev = asyncio.Event()

    def on_notify(self, _char, data: bytearray):
        self.chunks.append(bytes(data))
        vlog(f"RX {len(data):3d}B  {_hex(data)[:60]}")
        self.ev.set()

    def reset(self):
        self.chunks.clear()
        self.ev.clear()

    async def next_chunk(self, timeout):
        # Drain already-queued chunks first. The event only signals "something
        # arrived"; with back-to-back notifications a single Event coalesces, so
        # waiting on it when the queue is non-empty would strand later chunks.
        if not self.chunks:
            self.ev.clear()
            try:
                await asyncio.wait_for(self.ev.wait(), timeout)
            except asyncio.TimeoutError:
                return None
        return self.chunks.pop(0) if self.chunks else None


async def _measure(address, use_lx=False, outdir=None, no_plot=False, confirm=False):
    from bleak import BleakClient, BleakScanner

    if not address:
        hits = await _scan()
        if not hits:
            print("no HPCS device found", file=sys.stderr)
            return 2
        address = hits[0].address
        print(f"connecting {address}")

    rx = _Rx()
    async with BleakClient(address, timeout=20.0) as client:
        await client.start_notify(CHAR_UUID, rx.on_notify)

        async def send(frame):
            vlog(f"TX      {_hex(frame)}")
            await client.write_gatt_char(CHAR_UUID, frame, response=False)

        async def wait_ack(match, timeout=3.0):
            """Drain chunks until one starts with `match` (bytes, or a tuple of
            alternative prefixes); return it or None."""
            matches = (match,) if isinstance(match, bytes) else tuple(match)
            end = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < end:
                c = await rx.next_chunk(timeout=end - asyncio.get_event_loop().time())
                if c and any(c[:len(m)] == m for m in matches):
                    return c
            return None

        try:
            # 1) device info (some firmwares answer 0x8C EE, others 0x8C 00)
            rx.reset(); await send(CMD_INFO)
            c = await wait_ack((b"\x8c\xee", b"\x8c\x00"))
            info = decode_info(c) if c else {"name": "", "sn": None, "iVer": 0,
                                             "battery": None, "type": 0}
            dev_type = info["type"]
            print(f"device : {info['name']}  type={dev_type}  fw={info['iVer']}")

            # optional: wait for the user before triggering the measurement
            if confirm:
                await asyncio.to_thread(
                    input, "connected; position the device, then press Enter to measure...")

            # 2) integration time
            rx.reset(); await send(CMD_INTEG)
            it = await wait_ack(b"\x8c\x05")
            integ_ms = None
            if it and len(it) >= 6:
                integ_us = struct.unpack_from("<I", it, 2)[0]
                integ_ms = integ_us / 1000
                vlog(f"integration time = {integ_us} us ({integ_ms:g} ms)")

            # 3) start sampling
            rx.reset()
            await send(CMD_START_LX if use_lx else CMD_START)
            await wait_ack(b"\x8c\x0e")

            # 4) poll sample state until ready (byte[3]==1)
            ready = False
            for i in range(200):
                rx.reset(); await send(CMD_STATE)
                st = await wait_ack(b"\x8c\x03", timeout=1.0)
                vlog(f"poll #{i}: state byte = {st[3] if st and len(st) >= 4 else '?'}")
                if st and len(st) >= 4 and st[3] == 1:
                    ready = True
                    break
                await asyncio.sleep(0.1)
            if not ready:
                print("sample never became ready", file=sys.stderr)
                return 3

            # 5) read streamed result and reassemble.
            # The device streams the frame as many ~20-byte notifications. Drain on
            # an IDLE timeout (give up only after real silence), not a wall-clock
            # deadline, so a late final fragment still lands. On the first stall the
            # transfer is restarted once by re-sending CMD_RESULT.
            rx.reset(); await send(CMD_RESULT)
            buf = b""
            W = None
            HARD_CAP = 30.0            # absolute safety ceiling
            IDLE = 4.0                 # silence tolerance; the last 4-byte fragment lags
            hard_end = asyncio.get_event_loop().time() + HARD_CAP
            idle_hits = 0
            while asyncio.get_event_loop().time() < hard_end:
                c = await rx.next_chunk(timeout=IDLE)
                if not c:
                    idle_hits += 1
                    if idle_hits >= 2:
                        break          # genuinely quiet: give up
                    # stalled once: restart the transfer from scratch
                    vlog(f"stall with {len(buf)} bytes; re-sending result request")
                    rx.reset(); buf = b""; W = None
                    await send(CMD_RESULT)
                    continue
                idle_hits = 0
                if not buf:
                    if c[:2] != b"\x8c\x13" or len(c) < 4:
                        continue
                    # header u16 (big-endian) is payload length; total frame = len + 4
                    W = struct.unpack_from(">H", c, 2)[0] + 4
                    if W < 40 + RESULT_TAIL:   # same floor decode_result enforces
                        vlog(f"implausible result length {W}; ignoring chunk")
                        W = None
                        continue
                buf += c
                vlog(f"reassemble {len(buf):4d}/{W} bytes")
                if len(buf) >= W:
                    break

            if not (W and len(buf) >= W):
                if W is None:
                    print("incomplete result: no result header received", file=sys.stderr)
                else:
                    print(f"incomplete result: got {len(buf)} of {W} bytes", file=sys.stderr)
                return 4

            frame = buf[:W]
            vlog(f"frame W={W}  header={_hex(frame[:20])}")
            vlog(f"trailer last 16B @ {W-16}: {_hex(frame[W-16:])}")
            try:
                res = decode_result(frame, dev_type, info.get("iVer") or 0)
            except ValueError as e:
                print(f"result decode failed: {e}", file=sys.stderr)
                return 4
            print_summary(res, info)

            # resolve the output folder (default: device + SN + timestamp)
            if outdir:
                folder = outdir
            else:
                sn = info.get("sn")
                folder = f"{_sanitize(info.get('name'))}_{sn if sn is not None else 'unknown'}" \
                         f"_{time.strftime('%Y%m%d-%H%M%S')}"
            written = write_outputs(folder, info, res, frame, integ_ms, address,
                                    make_plot=not no_plot)
            print(f"wrote {len(written)} files -> {folder}/")
            for p in written:
                print(f"    {os.path.basename(p)}")
            return 0
        finally:
            # best-effort stop on every exit path (success, timeout, Ctrl+C, BLE
            # error) so the device is not left sampling; shield so a cancelled
            # task still gets the write out before the client disconnects.
            try:
                await asyncio.shield(send(CMD_STOP))
            except BaseException:
                pass


def flicker_sampling_metadata(index):
    known = isinstance(index, int) and 0 <= index < len(FLICK_WINDOW_MS)
    return {
        "sampling_rate_index": index,
        "sample_rate_hz": FLICK_SAMPLE_RATES_HZ[index] if known else None,
        "acquisition_window_ms": FLICK_WINDOW_MS[index] if known else None,
    }


def flicker_waveform_diagnostics(samples):
    # Conservative host policy, NOT a calibrated ADC clipping specification.
    low, high = 16, int(65535 * 0.99)
    result = {"sample_count": len(samples), "samples": samples,
              "low_guard_counts": low, "high_guard_counts": high,
              "threshold_basis": "conservative uint16 rail guards; uncalibrated"}
    if not samples:
        return dict(result, viable=False, saturated=False, reason="missing waveform")
    minimum, maximum = min(samples), max(samples)
    mean = statistics.mean(samples)
    rail_count = sum(x <= low or x >= high for x in samples)
    result.update(min=minimum, max=maximum, mean=mean,
                  peak_to_peak=maximum-minimum, sd=statistics.pstdev(samples),
                  rail_sample_count=rail_count, saturated=bool(rail_count),
                  viable=len(samples) == 400 and not rail_count and maximum > minimum,
                  reason="complete varying waveform" if len(samples) == 400 and not rail_count
                  and maximum > minimum else "incomplete, flat, or rail-limited waveform")
    return result


def flicker_decision(metrics, waveform, actual_gain, requested_gain=None):
    """Pure decision policy; zero metrics alone never authorize gain escalation."""
    if waveform.get("saturated"):
        return {"action": "reject", "reason": "waveform reached conservative rail guard"}
    if not waveform.get("viable"):
        return {"action": "reject", "reason": "waveform is not viable"}
    if metrics is None or not all(math.isfinite(v) for v in metrics.values()):
        return {"action": "reject", "reason": "missing or non-finite instrument metrics"}
    freq, percent, index, cycle = (metrics[k] for k in
        ("frequency_hz", "flicker_percent", "flicker_index", "flicker_cycle_ms"))
    if freq > 0 and cycle > 0 and 0 <= percent <= 100 and 0 <= index <= 1 \
            and abs(freq * cycle - 1000) <= 50:
        return {"action": "accept", "reason": "valid instrument metrics with reciprocal frequency/period"}
    if any(v != 0 for v in metrics.values()):
        return {"action": "reject", "reason": "nonzero but inconsistent instrument metrics"}
    # The gain register remains readable while Auto is enabled, but it is not a
    # reliable statement of the active automatic gain (the physical run read 03
    # in Auto). Begin manual fallback explicitly at x10, as the protocol plan
    # requires, rather than falsely considering Auto exhausted at x1000.
    if requested_gain is None:
        candidates = [1]
        base_gain = 1
    elif actual_gain not in range(4):
        return {"action": "reject", "reason": "unknown manual gain; cannot safely escalate"}
    else:
        candidates = [g for g in (1, 2, 3) if g > requested_gain]
        base_gain = actual_gain
    if not candidates:
        return {"action": "reject", "reason": "zero metrics; no higher gain remains"}
    gain = candidates[0]
    factor = 10 ** (gain - base_gain)
    # DC includes an unknown offset (~4000 in reference captures). Scale the AC
    # excursion, not the whole count. This estimate cannot guarantee no clipping.
    projected_low = waveform["mean"] - factor * (waveform["mean"] - waveform["min"])
    projected_high = waveform["mean"] + factor * (waveform["max"] - waveform["mean"])
    decision = {"next_gain_index": gain, "projected_min": projected_low,
                "projected_max": projected_high,
                "projection_basis": "AC excursion scaled by manual gain ratio around current mean; Auto register is diagnostic only; DC shift unknown"}
    if projected_low <= waveform["low_guard_counts"] or projected_high >= waveform["high_guard_counts"]:
        return dict(decision, action="reject", reason="insufficient estimated headroom for next gain")
    return dict(decision, action="retry", reason="zero metrics with viable waveform and estimated headroom")


def save_flicker_evidence(folder, record):
    """Checkpoint all attempts, including partial/failed attempts, after each change."""
    os.makedirs(folder, exist_ok=True)
    for attempt in record["attempts"]:
        destination = os.path.join(folder, "attempt_%02d" % attempt["number"])
        os.makedirs(destination, exist_ok=True)
        for key, filename in (("raw_3c_hex", "flicker_params.bin"),
                              ("raw_3a_hex", "flicker_waveform.bin")):
            with open(os.path.join(destination, filename), "wb") as stream:
                stream.write(bytes.fromhex(attempt[key]))
        with open(os.path.join(destination, "flicker_waveform.csv"), "w") as stream:
            stream.write("sample,value\n")
            for i, value in enumerate(attempt["waveform"]["samples"]):
                stream.write(f"{i},{value}\n")
        with open(os.path.join(destination, "attempt.json"), "w") as stream:
            json.dump(attempt, stream, indent=2, allow_nan=False)
    temporary = os.path.join(folder, "flicker.json.tmp")
    with open(temporary, "w") as stream:
        json.dump(record, stream, indent=2, allow_nan=False)
    os.replace(temporary, os.path.join(folder, "flicker.json"))


async def _flicker(address, outdir=None, auto_settings=False, continuous=False):
    """Auto-first adaptive capture; never discard a failed or rejected attempt."""
    from bleak import BleakClient
    if not address:
        hits = await _scan()
        if not hits:
            print("no HPCS device found", file=sys.stderr)
            return 2
        address = hits[0].address
    rx = _Rx()
    async with BleakClient(address, timeout=20.0) as client:
        await client.start_notify(CHAR_UUID, rx.on_notify)

        async def send(frame):
            vlog("TX", _hex(frame))
            await client.write_gatt_char(CHAR_UUID, frame, response=False)

        async def request(frame, size=None, timeout=3.0, progress=None):
            rx.reset()
            await send(frame)
            received = b""
            end = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < end:
                chunk = await rx.next_chunk(end - asyncio.get_running_loop().time())
                if not chunk:
                    break
                if not received and not chunk.startswith(frame[:2]):
                    continue
                received += chunk
                if progress:
                    progress(received)
                if size is None or len(received) >= size:
                    break
            return received

        async def config():
            result = {}
            for label, command in (("gain_mode", CMD_FLICK_GEAR_MODE),
                                   ("gain_index", CMD_FLICK_GEAR),
                                   # 8C3F is read by the APK, but it is not a
                                   # validated echo of the 8C41 argument. Preserve
                                   # the byte as an opaque status observation.
                                   ("rate_status_3f", CMD_FLICK_RATE_MODE),
                                   ("rate_index", CMD_FLICK_SPEED)):
                raw = await request(command, 3)
                result[label] = raw[2] if len(raw) == 3 else None
                result[label + "_raw_hex"] = raw.hex()
            return result

        folder = outdir or ("HPCS_flicker_" + time.strftime('%Y%m%d-%H%M%S'))
        if os.path.exists(os.path.join(folder, "flicker.json")) or \
                (os.path.isdir(folder) and any(n.startswith("attempt_") for n in os.listdir(folder))):
            raise ValueError("output folder already contains flicker evidence; choose a fresh --out")
        record = {"schema_version": 2, "address": address,
                  "captured_at": time.strftime('%Y-%m-%dT%H:%M:%S'),
                  "automatic_gain_requested": True, "automatic_rate_requested": auto_settings,
                  "continuous_mode_requested": continuous,
                  "start_command": "8c0e04" if continuous else "8c0e03",
                  "attempts": [], "accepted_attempt": None, "status": "in_progress"}
        initial = None
        changed = False
        try:
            raw = await request(CMD_INFO, 20)
            record["device"] = decode_info(raw) if len(raw) >= 20 else {}
            if continuous and record["device"].get("iVer", 0) < 2007:
                raise ValueError("continuous mode requires verified firmware >=2007")
            initial = await config()
            record["initial_config"] = initial
            if initial["gain_mode"] not in (0, 1) or initial["gain_index"] not in range(4) \
                    or initial["rate_status_3f"] not in (0, 1) or initial["rate_index"] not in range(11):
                raise ValueError("initial configuration incomplete; cannot safely restore settings")
            gain = None
            for number in range(1, 5):
                attempt = {"number": number, "requested_gain_mode": "auto" if gain is None else "manual",
                           "requested_gain_index": gain,
                           "requested_gain_multiplier": None if gain is None else 10 ** gain,
                           "raw_3c_hex": "", "raw_3a_hex": "", "metrics": None,
                           "waveform": flicker_waveform_diagnostics([]),
                           "decision": {"action": "reject", "reason": "attempt incomplete"}}
                record["attempts"].append(attempt)
                save_flicker_evidence(folder, record)
                changed = True
                await send(CMD_STOP)
                await asyncio.sleep(0.15)
                await request(bytes([0x8c, 0x37, 1 if gain is None else 0]))
                if gain is not None:
                    await request(bytes([0x8c, 0x35, gain]))
                if number == 1 and auto_settings:
                    await request(CMD_FLICK_RATE_AUTO)
                await asyncio.sleep(0.15)
                attempt["config_before"] = await config()
                before = attempt["config_before"]
                if before["gain_mode"] != (1 if gain is None else 0) or \
                        before["gain_index"] not in range(4) or \
                        (gain is not None and before["gain_index"] != gain) or \
                        before["rate_status_3f"] not in (0, 1) or before["rate_index"] not in range(11):
                    raise ValueError("requested settings not confirmed by readback")
                attempt.update(flicker_sampling_metadata(before["rate_index"]))
                save_flicker_evidence(folder, record)
                start_command = CMD_FLICK_START_CONTINUOUS if continuous else CMD_FLICK_START
                start_ack = await request(start_command)
                attempt["start_ack_hex"] = start_ack.hex()
                # The 310P returns a bare two-byte 8C 0E acknowledgement. The
                # queue was reset immediately before this write and request()
                # accepts only this command prefix, so this preserves the stale-
                # result guard without inventing a payload byte requirement.
                if start_ack[:2] != start_command[:2]:
                    raise TimeoutError("missing flicker start acknowledgement; refusing stale result")
                if continuous:
                    await asyncio.sleep(0.2)
                    attempt["battery_status_hex"] = (await request(CMD_BATT, timeout=1)).hex()
                    await asyncio.sleep(0.8)
                # Wait at least the configured window; a ready bit may be stale
                # immediately after start. Allow the longest APK window as well.
                await asyncio.sleep(attempt["acquisition_window_ms"] / 1000)
                ready = False
                for _ in range(200):
                    state = await request(CMD_FLICK_STATE, 3, timeout=1)
                    attempt["last_state_hex"] = state.hex()
                    if len(state) == 3 and state[2] == 1:
                        ready = True
                        break
                    await asyncio.sleep(0.1)
                if not ready:
                    raise TimeoutError("flicker acquisition never became ready")

                def retain(key):
                    def update(raw):
                        attempt[key] = raw.hex()
                        if key == "raw_3a_hex":
                            count = min(400, max(0, (len(raw) - 2) // 2))
                            samples = list(struct.unpack_from(f"<{count}H", raw, 2)) if count else []
                            attempt["waveform"] = flicker_waveform_diagnostics(samples)
                    return update

                params = await request(CMD_FLICK_PARAMS, 18, progress=retain("raw_3c_hex"))
                if len(params) == 18:
                    values = struct.unpack_from("<4f", params, 2)
                    attempt["metrics"] = dict(zip(("frequency_hz", "flicker_percent",
                        "flicker_index", "flicker_cycle_ms"), values)) if all(map(math.isfinite, values)) else None
                    if not all(map(math.isfinite, values)):
                        attempt["metric_decode_error"] = "non-finite float32; raw response retained"
                wave = await request(CMD_FLICK_WAVE, 802, timeout=8, progress=retain("raw_3a_hex"))
                if len(wave) != 802:
                    attempt["waveform"].update(viable=False, reason="incorrect waveform frame length")
                attempt["config_after"] = await config()
                actual = attempt["config_after"]
                attempt.update(flicker_sampling_metadata(actual["rate_index"]))
                if actual["gain_mode"] != before["gain_mode"] or actual["gain_index"] not in range(4) \
                        or (gain is not None and actual["gain_index"] != gain) \
                        or actual["rate_status_3f"] != before["rate_status_3f"] or actual["rate_index"] not in range(11) \
                        or (before["rate_index"] != actual["rate_index"]):
                    attempt["decision"] = {"action": "reject", "reason": "invalid post-acquisition configuration readback"}
                else:
                    attempt["decision"] = flicker_decision(attempt["metrics"], attempt["waveform"],
                                                           actual["gain_index"], gain)
                await send(CMD_STOP)
                decision = attempt["decision"]
                print(f"attempt {number}: {decision['action']} — {decision['reason']}")
                if decision["action"] == "accept":
                    record["accepted_attempt"] = number
                    record["metrics"] = attempt["metrics"]
                    record["status"] = "accepted"
                elif decision["action"] == "reject":
                    record["status"] = "rejected"
                save_flicker_evidence(folder, record)
                if decision["action"] != "retry":
                    return 0 if decision["action"] == "accept" else 6
                gain = decision["next_gain_index"]
            record["status"] = "rejected"
            return 6
        except BaseException as error:
            record["status"] = "error"
            record["error"] = f"{type(error).__name__}: {error}"
            if record["attempts"]:
                record["attempts"][-1]["decision"] = {"action": "reject", "reason": record["error"]}
            raise
        finally:
            async def cleanup():
                results = []
                commands = [CMD_STOP]
                if changed and initial:
                    commands += [bytes([0x8c, 0x37, 0]), bytes([0x8c, 0x35, initial["gain_index"]]),
                                 bytes([0x8c, 0x37, initial["gain_mode"]])]
                    if auto_settings:
                        commands += [bytes([0x8c, 0x41, 0]), bytes([0x8c, 0x3e, initial["rate_index"]]),
                                     # The original 8C3F byte remains the legacy
                                     # restore convention; its semantics are recorded
                                     # as unresolved rather than treated as verified.
                                     bytes([0x8c, 0x41, initial["rate_status_3f"]])]
                for command in commands:
                    try:
                        await send(command)
                        await asyncio.sleep(0.15)
                        results.append({"command": command.hex(), "sent": True})
                    except Exception as error:
                        results.append({"command": command.hex(), "error": str(error)})
                record["cleanup"] = results
                try:
                    record["restored_config"] = await config()
                    restored = record["restored_config"]
                    record["restoration_verified"] = bool(initial) and all(
                        restored[k] == initial[k] for k in ("gain_mode", "rate_status_3f")) and \
                        (initial["gain_mode"] == 1 or restored["gain_index"] == initial["gain_index"]) and \
                        restored["rate_index"] == initial["rate_index"]
                except Exception as error:
                    record["restoration_verified"] = False
                    record["cleanup_error"] = str(error)
            task = asyncio.create_task(cleanup())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            finally:
                save_flicker_evidence(folder, record)
                print(f"wrote all flicker attempts -> {folder}")
                if not record.get("restoration_verified"):
                    print("WARNING: settings restoration not verified; inspect flicker.json", file=sys.stderr)


# ----------------------------------------------------------------------------
# Self-test (no hardware): build a synthetic frame, round-trip the decoder.
# ----------------------------------------------------------------------------
def _selftest():
    import math
    start_wave, end_wave = 380.0, 780.0
    n_valid = int(end_wave - start_wave) + 1          # 401
    # synthetic spectrum: gaussian peak at 550 nm
    spec = [0.0] * N_SPECTRUM
    for i in range(n_valid):
        wl = start_wave + i
        spec[i] = math.exp(-((wl - 550.0) ** 2) / (2 * 40.0 ** 2))

    metric_floats = 60                                # arbitrary metric count
    W = 40 + metric_floats * 4 + RESULT_TAIL
    frame = bytearray(W)
    frame[0] = 0x8C
    frame[1] = 0x13
    struct.pack_into(">H", frame, 2, W - 4)           # payload length (big-endian)
    struct.pack_into("<f", frame, 40, 1234.5)         # fLx (default type field 0)
    spec_off = W - RESULT_TAIL
    struct.pack_into("<%df" % N_SPECTRUM, frame, spec_off, *spec)
    struct.pack_into("<f", frame, W - 8, start_wave)
    struct.pack_into("<f", frame, W - 4, end_wave)

    res = decode_result(bytes(frame), dev_type=0)
    ok = True
    ok &= abs(res["start_wave"] - 380.0) < 1e-3
    ok &= abs(res["end_wave"] - 780.0) < 1e-3
    ok &= res["n_valid"] == 401
    ok &= abs(res["wavelengths"][0] - 380.0) < 1e-6
    ok &= abs(res["wavelengths"][-1] - 780.0) < 1e-6
    peak_i = max(range(res["n_valid"]), key=lambda i: res["intensities"][i])
    ok &= abs(res["wavelengths"][peak_i] - 550.0) < 1.0
    ok &= abs(res["metrics"].get("fLx", 0) - 1234.5) < 1e-2

    # info decoder check
    info_frame = bytearray(20)
    info_frame[0] = 0x8C; info_frame[1] = 0xEE
    nm = b"HPCS310C"
    info_frame[2:2+len(nm)] = nm
    struct.pack_into("<I", info_frame, 12, 100123)
    struct.pack_into("<H", info_frame, 16, 2007)
    info_frame[18] = 87; info_frame[19] = 1
    info = decode_info(info_frame)
    ok &= info["name"] == "HPCS310C" and info["sn"] == 100123
    ok &= info["iVer"] == 2007 and info["battery"] == 87 and info["type"] == 5

    print_summary(res, info)
    print("SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ----------------------------------------------------------------------------
def main(argv=None):
    global VERBOSE
    # shared parent so -v works before OR after the subcommand
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("-v", "--verbose", action="store_true",
                        default=argparse.SUPPRESS,
                        help="log every BLE frame (hex), handshake steps and metrics")

    ap = argparse.ArgumentParser(description="HopooColor HPCS-310/330 BLE spectrum reader",
                                 parents=[parent])
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan", parents=[parent], help="scan for HPCS* BLE devices")

    fli = sub.add_parser("flicker", parents=[parent], help="acquire flicker metrics and temporal waveform")
    fli.add_argument("--address", help="BLE MAC/address (default: first HPCS* found)")
    fli.add_argument("--out", help="output folder")
    fli.add_argument("--auto", action="store_true",
                     help="request automatic sample rate; adaptive capture always starts with Auto gain")
    fli.add_argument("--continuous", action="store_true",
                     help="use the official firmware >=2007 continuous flicker start command 0x8C0E04")

    m = sub.add_parser("measure", parents=[parent],
                       help="connect, measure, dump a full result folder")
    m.add_argument("--address", help="BLE MAC/address (default: first HPCS* found)")
    m.add_argument("--out", help="output folder (default: device_SN_timestamp/, auto-created)")
    m.add_argument("--lx", action="store_true", help="use 0x8C0E02 (Lx) start variant")
    m.add_argument("--no-plot", action="store_true", help="skip rendering spectrum.png")
    m.add_argument("--confirm", action="store_true",
                   help="connect, then wait for Enter before measuring")

    q = sub.add_parser("quality", parents=[parent], help="derive TM-30, CQS and TLCI from a saved measurement")
    q.add_argument("measurement", help="measurement.json containing stored SPD")
    q.add_argument("--out", help="output JSON (default: derived_metrics.json beside measurement)")
    q.add_argument("--ssi-reference", help="explicit Colour illuminant name for Academy SSI, e.g. D65")

    d = sub.add_parser("decode", parents=[parent],
                       help="decode a captured raw result frame from a file")
    d.add_argument("file")
    d.add_argument("--type", type=int, default=0, help="device type code (0-5)")
    d.add_argument("--iver", type=int, default=0,
                   help="firmware version for metric naming (see measurement.json 'firmware')")
    d.add_argument("--csv")

    sub.add_parser("selftest", parents=[parent], help="run parser self-check (no hardware)")

    args = ap.parse_args(argv)
    VERBOSE = getattr(args, "verbose", False)

    if args.cmd == "scan":
        return 0 if asyncio.run(_scan()) else 1
    if args.cmd == "measure":
        return asyncio.run(_measure(args.address, args.lx, args.out,
                                    args.no_plot, args.confirm)) or 0
    if args.cmd == "flicker":
        return asyncio.run(_flicker(args.address, args.out, args.auto, args.continuous)) or 0
    if args.cmd == "quality":
        out, payload = write_derived_metrics(args.measurement, args.out, args.ssi_reference)
        dm = payload["derived_metrics"]
        print(f"TM-30-18: Rf={dm['tm30_18']['Rf']:.2f}  Rg={dm['tm30_18']['Rg']:.2f}")
        print(f"CQS 9.0: {dm['cqs_nist_9_0']:.2f}  TLCI-2012: {dm['tlci_2012']:.2f}")
        if dm.get("ssi"):
            print(f"SSI vs {dm['ssi']['reference']}: {dm['ssi']['value']:.1f}")
        print(f"wrote {out}")
        return 0
    if args.cmd == "decode":
        buf = open(args.file, "rb").read()
        # a saved capture may or may not include the 4-byte header; require it
        res = decode_result(buf, args.type, args.iver)
        print_summary(res)
        if args.csv:
            write_csv(args.csv, res)
            print(f"wrote {args.csv}  ({res['n_valid']} rows)")
        return 0
    if args.cmd == "selftest":
        return _selftest()
    return 1


if __name__ == "__main__":
    sys.exit(main())
