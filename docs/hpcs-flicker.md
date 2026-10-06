# HPCS-310P flicker protocol and current experiment status

This record describes the HPCS-005 adaptive flicker acquisition and the
separate HPCS-006 gain/sensitivity characterisation runner. It distinguishes
confirmed device observations from host-side safeguards and unresolved protocol
semantics.

## Confirmed protocol behavior

| Command | Confirmed behavior |
| --- | --- |
| `8C 0E 03` | Starts a single flicker acquisition. The observed acknowledgement can be the bare two-byte prefix `8C 0E`. |
| `8C 0E 04` | Continuous flicker start path; the CLI permits it only on firmware 2007 or newer. |
| `8C 3B` | Acquisition readiness readback. |
| `8C 3C` | 18-byte frame containing four little-endian float32 values: frequency, flicker percent, flicker index, and cycle time. |
| `8C 3A` | 802-byte frame containing a two-byte prefix and 400 little-endian uint16 waveform samples. |
| `8C 37 01/00`, `8C 35 00..03` | Auto/manual gain selection and manual ×1/×10/×100/×1000 selection. `8C 38` and `8C 36` provide gain-mode and gain-index readbacks. |
| `8C 41 00`, `8C 3E 07` | Accepted rate-control and index-setting commands for the HPCS-006 fixed 20 kHz / 500 ms setup. `8C 3D=07` confirms the selected index. |
| `8C 25` | Stops flicker acquisition. |

The rate-index mapping records the APK acquisition window separately from the
sample rate. Index `07` is **20,000 Hz** with a **500 ms** acquisition window.
The displayed `8C3A` waveform remains fixed at 400 samples; it is not the full
internal acquisition record.

## `8C3F` is unresolved

The APK reads `8C3F`, but current evidence does not establish that its byte is a
rate-mode echo of the `8C41` argument. In the HPCS-006 preflight the instrument
acknowledged `8C4100` and `8C3E07`, then returned `8C3F=01` while `8C3D=07`.

Software therefore preserves this value as `rate_status_3f` rather than calling
it a verified rate mode. The fixed-rate preflight requires fresh setter
acknowledgements, manual gain readback, and `8C3D=07`; it records `8C3F` for
later protocol work. Restoration records the same limitation. No sensitivity or
configuration claim is inferred from `8C3F`.

## HPCS-005 behavior

`hpcs310.py flicker` uses Auto gain first. A zero `8C3C` result alone does not
increase gain: the 400-sample waveform must be complete, varying, and below
conservative host rail guards. It then tries manual ×10, ×100, and ×1000 only
when the next gain is projected to retain headroom. These rail guards are host
heuristics, not calibrated HPCS clipping limits.

Every attempt contains requested and read-back configuration, acquisition
metadata, raw `8C3C` and `8C3A` bytes, all 400 waveform values, waveform
statistics, and its decision. The stale-result guard resets the notification
queue immediately before the start command and accepts only a fresh `8C0E`
prefix acknowledgement.

The physical HPCS-005 sequence completed the Auto, ×10, ×100, ×1000 protocol
path with complete, varying, unsaturated waveforms. All four attempts returned
zero `8C3C` metrics, so it did not reproduce the earlier approximately 100 Hz
reading and was correctly not accepted as a flicker measurement.

## HPCS-006 current status

`hpcs006_gain.py` is an independent sequential manual-gain experiment. For
each gain it retains raw `8C3A`/`8C3C`, all waveform samples, configuration
readbacks, min/max/mean/SD/peak-to-peak, modulation, digital headroom,
provisional clipping diagnostics, and `8C3C` detection status.

The first physical HPCS-006 run stopped before collecting a waveform because
the earlier code incorrectly required `8C3F=00`. It produced no ×1/×10/×100/×1000
measurement results and establishes no sensitivity, noise-floor, dynamic-range,
or low-flicker-discrimination specification. The corrected runner is ready for
the next physical run and does not change HPCS-005's decision policy.

Raw capture directories are intentionally excluded from Git. They remain the
source evidence for experimental records and must be banked separately after a
completed physical sequence.
