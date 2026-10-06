#!/usr/bin/env python3
"""HPCS-006: fixed-rate manual gain/sensitivity characterisation.

This is intentionally separate from HPCS-005's adaptive acquisition path.
It captures all four manual gain settings at rate index 07 (20 kHz / 500 ms),
and retains raw evidence even when the instrument reports no flicker.
"""
import argparse
import asyncio
import json
import math
import os
import statistics
import struct
import sys
import time

import hpcs310 as hpcs

GAIN_MULTIPLIERS = (1, 10, 100, 1000)
RATE_INDEX = 7
CMD_GAIN_MANUAL = bytes((0x8C, 0x37, 0x00))
CMD_GAIN_SET = 0x35
CMD_RATE_MANUAL = bytes((0x8C, 0x41, 0x00))
CMD_RATE_SET = 0x3E


def finite_metrics(raw):
    if len(raw) != 18:
        return None
    values = struct.unpack_from("<4f", raw, 2)
    if not all(math.isfinite(value) for value in values):
        return None
    return dict(zip(("frequency_hz", "flicker_percent", "flicker_index", "flicker_cycle_ms"), values))


def valid_detection(metrics):
    if not metrics:
        return False
    frequency = metrics["frequency_hz"]
    cycle = metrics["flicker_cycle_ms"]
    return (frequency > 0 and cycle > 0 and 0 <= metrics["flicker_percent"] <= 100
            and 0 <= metrics["flicker_index"] <= 1 and abs(frequency * cycle - 1000) <= 50)


def sensitivity_diagnostics(samples):
    """Report signal observables without claiming an uncalibrated noise spec."""
    result = hpcs.flicker_waveform_diagnostics(samples)
    if not samples:
        return result
    minimum, maximum, mean = result["min"], result["max"], result["mean"]
    differences = [samples[i] - samples[i - 1] for i in range(1, len(samples))]
    # This includes real high-frequency signal content. It is useful for comparing
    # these four captures, but is not a standalone electronic noise-floor measure.
    diff_rms = math.sqrt(statistics.mean(value * value for value in differences)) if differences else 0.0
    result.update({
        "dc_mean_counts": mean,
        "ac_peak_to_peak_counts": maximum - minimum,
        "ac_half_peak_to_peak_counts": (maximum - minimum) / 2,
        "waveform_modulation_percent": 100 * (maximum - minimum) / (maximum + minimum)
        if maximum + minimum else None,
        "successive_difference_rms_counts": diff_rms,
        "noise_proxy_note": "successive-difference RMS includes temporal signal; it is only a within-waveform comparison proxy",
        "noise_floor_status": "not calibrated; requires repeat captures and/or a dark/reference experiment",
        "digital_headroom": {
            "low_rail_counts": minimum,
            "high_rail_counts": 65535 - maximum,
            "low_guard_margin_counts": minimum - result["low_guard_counts"],
            "high_guard_margin_counts": result["high_guard_counts"] - maximum,
            "high_rail_percent_of_full_scale": 100 * (65535 - maximum) / 65535,
            "near_uint16_low_rail": minimum <= result["low_guard_counts"],
            "near_uint16_high_rail": maximum >= result["high_guard_counts"],
        },
    })
    return result


def characterisation_summary(captures):
    """Build observational comparisons; no sensitivity specification is inferred."""
    reference = next((capture for capture in captures if capture["gain_index"] == 0), None)
    comparison = []
    for capture in captures:
        waveform = capture["waveform"]
        ratios = {}
        if reference and reference["waveform"].get("sample_count") == 400:
            for field in ("dc_mean_counts", "ac_peak_to_peak_counts", "sd"):
                base = reference["waveform"].get(field)
                value = waveform.get(field)
                ratios[field + "_vs_x1"] = value / base if base not in (None, 0) and value is not None else None
        comparison.append({
            "gain_index": capture["gain_index"],
            "gain_multiplier": capture["gain_multiplier"],
            "instrument_3c_detected": capture["instrument_3c_detected"],
            "metrics": capture["metrics"],
            "clipping_diagnostics": {
                "saturated": waveform.get("saturated"),
                "digital_headroom": waveform.get("digital_headroom"),
            },
            "gain_ratios_vs_x1": ratios,
        })
    detected = [capture for capture in captures if capture["instrument_3c_detected"]]
    clipped = [capture for capture in captures if capture["waveform"].get("saturated")]
    return {
        "gain_comparison": comparison,
        "first_instrument_detection": (detected[0]["gain_multiplier"] if detected else None),
        "instrument_detection_status": "detected" if detected else "no valid 3C detection in this sequence",
        "first_observed_clipping_gain": (clipped[0]["gain_multiplier"] if clipped else None),
        "clipping_status": "observed" if clipped else "not observed by provisional uint16 rail guards",
        "practical_lowest_reliably_distinguishable_flicker": {
            "value": None,
            "status": "not established by one four-gain sequence",
            "reason": "Requires repeatability and a source with independently known modulation; zero 3C is evidence, not a numeric sensitivity limit.",
        },
        "limitations": [
            "3A is a fixed 400-point display waveform, not the full internal acquisition record.",
            "Rail guards are provisional host heuristics, not calibrated HPCS clipping limits.",
            "The single sequence cannot separate source drift, temporal waveform content, and electronic noise into a calibrated noise floor.",
        ],
    }


def write_evidence(folder, record):
    os.makedirs(folder, exist_ok=True)
    for capture in record["captures"]:
        capture_dir = os.path.join(folder, "gain_x%d" % capture["gain_multiplier"])
        os.makedirs(capture_dir, exist_ok=True)
        for key, name in (("raw_3c_hex", "flicker_params.bin"), ("raw_3a_hex", "flicker_waveform.bin")):
            with open(os.path.join(capture_dir, name), "wb") as stream:
                stream.write(bytes.fromhex(capture[key]))
        with open(os.path.join(capture_dir, "flicker_waveform.csv"), "w") as stream:
            stream.write("sample,value\n")
            for number, value in enumerate(capture["waveform"]["samples"]):
                stream.write(f"{number},{value}\n")
        with open(os.path.join(capture_dir, "capture.json"), "w") as stream:
            json.dump(capture, stream, indent=2, allow_nan=False)
    temporary = os.path.join(folder, "hpcs006.json.tmp")
    with open(temporary, "w") as stream:
        json.dump(record, stream, indent=2, allow_nan=False)
    os.replace(temporary, os.path.join(folder, "hpcs006.json"))


async def run(address, output):
    from bleak import BleakClient
    if os.path.exists(os.path.join(output, "hpcs006.json")):
        raise ValueError("output already contains HPCS-006 evidence; choose a fresh --out")
    rx = hpcs._Rx()
    async with BleakClient(address, timeout=20.0) as client:
        await client.start_notify(hpcs.CHAR_UUID, rx.on_notify)

        async def send(frame):
            hpcs.vlog("TX", hpcs._hex(frame))
            await client.write_gatt_char(hpcs.CHAR_UUID, frame, response=False)

        async def request(frame, expected_size=None, timeout=3.0, progress=None):
            rx.reset()
            await send(frame)
            received = b""
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                chunk = await rx.next_chunk(deadline - asyncio.get_running_loop().time())
                if not chunk:
                    break
                if not received and not chunk.startswith(frame[:2]):
                    continue
                received += chunk
                if progress:
                    progress(received)
                if expected_size is None or len(received) >= expected_size:
                    break
            return received

        async def read_config():
            config = {}
            for name, command in (("gain_mode", hpcs.CMD_FLICK_GEAR_MODE),
                                  ("gain_index", hpcs.CMD_FLICK_GEAR),
                                  # The APK reads 8C3F, but experiments have not
                                  # established it as an echo of the 8C41 argument.
                                  # Preserve it as an opaque status byte; 8C3D is the
                                  # proven confirmation of the selected rate index.
                                  ("rate_status_3f", hpcs.CMD_FLICK_RATE_MODE),
                                  ("rate_index", hpcs.CMD_FLICK_SPEED)):
                raw = await request(command, 3)
                config[name] = raw[2] if len(raw) == 3 else None
                config[name + "_raw_hex"] = raw.hex()
            return config

        record = {"experiment": "HPCS-006 — Flicker Gain/Sensitivity Characterisation",
                  "schema_version": 1, "address": address,
                  "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                  "planned_sequence": [1, 10, 100, 1000],
                  "fixed_rate": hpcs.flicker_sampling_metadata(RATE_INDEX),
                  "captures": [], "status": "in_progress"}
        initial = None
        changed = False
        try:
            info_raw = await request(hpcs.CMD_INFO, 20)
            record["device"] = hpcs.decode_info(info_raw) if len(info_raw) >= 20 else {}
            initial = await read_config()
            record["initial_config"] = initial
            if initial["gain_mode"] not in (0, 1) or initial["gain_index"] not in range(4) \
                    or initial["rate_status_3f"] not in (0, 1) or initial["rate_index"] not in range(11):
                raise ValueError("initial configuration incomplete; cannot guarantee restoration")

            # Mark the session changed before the first setter: a failure during
            # verification must still restore the initial gain/rate configuration.
            changed = True
            rate_manual_ack = await request(CMD_RATE_MANUAL)
            rate_index_ack = await request(bytes((0x8C, CMD_RATE_SET, RATE_INDEX)))
            gain_manual_ack = await request(CMD_GAIN_MANUAL)
            if (rate_manual_ack[:2] != CMD_RATE_MANUAL[:2]
                    or rate_index_ack[:2] != bytes((0x8C, CMD_RATE_SET))
                    or gain_manual_ack[:2] != CMD_GAIN_MANUAL[:2]):
                raise TimeoutError("missing configuration-set acknowledgement")
            configured = await read_config()
            if configured["rate_index"] != RATE_INDEX or configured["gain_mode"] != 0:
                raise ValueError("could not confirm manual 20 kHz / 500 ms mode")
            record["manual_fixed_config"] = {
                **configured,
                "rate_configuration_confirmation": {
                    "command_8c4100_ack_hex": rate_manual_ack.hex(),
                    "command_8c3e07_ack_hex": rate_index_ack.hex(),
                    "command_8c3700_ack_hex": gain_manual_ack.hex(),
                    "rate_index_8c3d": configured["rate_index"],
                    "rate_status_8c3f": configured["rate_status_3f"],
                    "status": "rate index confirmed; 8C3F semantics unresolved",
                },
            }

            for gain_index, gain_multiplier in enumerate(GAIN_MULTIPLIERS):
                capture = {"gain_index": gain_index, "gain_multiplier": gain_multiplier,
                           "requested_gain_mode": "manual", "requested_rate_configuration":
                           "8C4100 and 8C3E07 acknowledged; index confirmed by 8C3D",
                           **hpcs.flicker_sampling_metadata(RATE_INDEX),
                           "raw_3c_hex": "", "raw_3a_hex": "", "metrics": None,
                           "instrument_3c_detected": False,
                           "waveform": sensitivity_diagnostics([]),
                           "status": "in_progress"}
                record["captures"].append(capture)
                write_evidence(output, record)

                await request(hpcs.CMD_STOP)
                await asyncio.sleep(0.15)
                setting_ack = await request(bytes((0x8C, CMD_GAIN_SET, gain_index)))
                capture["gain_set_ack_hex"] = setting_ack.hex()
                await asyncio.sleep(0.15)
                capture["config_before"] = await read_config()
                config = capture["config_before"]
                if (config["gain_mode"], config["gain_index"], config["rate_index"]) != (0, gain_index, RATE_INDEX):
                    raise ValueError("manual gain/rate readback mismatch")

                start_ack = await request(hpcs.CMD_FLICK_START)
                capture["start_ack_hex"] = start_ack.hex()
                if start_ack[:2] != hpcs.CMD_FLICK_START[:2]:
                    raise TimeoutError("missing fresh 8C0E flicker-start acknowledgement")
                await asyncio.sleep(0.5)
                ready = False
                for _ in range(200):
                    state = await request(hpcs.CMD_FLICK_STATE, 3, timeout=1)
                    capture["last_state_hex"] = state.hex()
                    if len(state) == 3 and state[2] == 1:
                        ready = True
                        break
                    await asyncio.sleep(0.1)
                if not ready:
                    raise TimeoutError("flicker acquisition never became ready")

                def retain(key):
                    def update(raw):
                        capture[key] = raw.hex()
                        if key == "raw_3a_hex":
                            points = min(400, max(0, (len(raw) - 2) // 2))
                            values = list(struct.unpack_from(f"<{points}H", raw, 2)) if points else []
                            capture["waveform"] = sensitivity_diagnostics(values)
                    return update

                params = await request(hpcs.CMD_FLICK_PARAMS, 18, progress=retain("raw_3c_hex"))
                capture["metrics"] = finite_metrics(params)
                wave = await request(hpcs.CMD_FLICK_WAVE, 802, timeout=8, progress=retain("raw_3a_hex"))
                if len(wave) != 802:
                    capture["waveform"].update(viable=False, reason="incorrect waveform frame length")
                capture["instrument_3c_detected"] = valid_detection(capture["metrics"])
                capture["detection_status"] = ("valid instrument detection" if capture["instrument_3c_detected"]
                                               else "no valid instrument 3C detection")
                capture["config_after"] = await read_config()
                if any(capture["config_after"][name] != capture["config_before"][name]
                       for name in ("gain_mode", "gain_index", "rate_index")):
                    raise ValueError("configuration changed during acquisition")
                capture["status"] = "complete"
                await request(hpcs.CMD_STOP)
                write_evidence(output, record)

            record["analysis"] = characterisation_summary(record["captures"])
            record["status"] = "complete"
            return 0
        except BaseException as error:
            record["status"] = "error"
            record["error"] = f"{type(error).__name__}: {error}"
            if record["captures"]:
                record["captures"][-1]["status"] = "error"
            raise
        finally:
            async def restore():
                operations = [hpcs.CMD_STOP]
                if changed and initial:
                    operations += [CMD_GAIN_MANUAL, bytes((0x8C, CMD_GAIN_SET, initial["gain_index"]),),
                                   bytes((0x8C, 0x37, initial["gain_mode"]),), CMD_RATE_MANUAL,
                                   bytes((0x8C, CMD_RATE_SET, initial["rate_index"]),),
                                   # 8C3F is not a validated mode readback. This uses
                                   # the pre-run byte only as the established legacy
                                   # restore convention and records the limitation.
                                   bytes((0x8C, 0x41, initial["rate_status_3f"]),)]
                record["cleanup"] = []
                for operation in operations:
                    try:
                        await send(operation)
                        await asyncio.sleep(0.15)
                        record["cleanup"].append({"command": operation.hex(), "sent": True})
                    except Exception as error:
                        record["cleanup"].append({"command": operation.hex(), "error": str(error)})
                try:
                    record["restored_config"] = await read_config()
                    restored = record["restored_config"]
                    gain_verified = bool(initial) and restored["gain_mode"] == initial["gain_mode"] and \
                        (initial["gain_mode"] == 1 or restored["gain_index"] == initial["gain_index"])
                    rate_index_verified = bool(initial) and restored["rate_index"] == initial["rate_index"]
                    record["restoration_verified"] = gain_verified and rate_index_verified
                    record["restoration_verification"] = {
                        "gain_readbacks_restored": gain_verified,
                        "rate_index_8c3d_restored": rate_index_verified,
                        "rate_status_8c3f_before": initial["rate_status_3f"] if initial else None,
                        "rate_status_8c3f_after": restored["rate_status_3f"],
                        "rate_control_state": "not semantically verified: 8C3F meaning unresolved",
                    }
                except Exception as error:
                    record["restoration_verified"] = False
                    record["cleanup_error"] = str(error)
            task = asyncio.create_task(restore())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            finally:
                write_evidence(output, record)


def main(argv=None):
    parser = argparse.ArgumentParser(description="HPCS-006 fixed-rate manual gain characterisation")
    parser.add_argument("--address", required=True, help="HPCS BLE address")
    parser.add_argument("--out", required=True, help="fresh evidence directory")
    parser.add_argument("-v", "--verbose", action="store_true", help="log BLE frames")
    arguments = parser.parse_args(argv)
    hpcs.VERBOSE = arguments.verbose
    return asyncio.run(run(arguments.address, arguments.out))


if __name__ == "__main__":
    sys.exit(main())
