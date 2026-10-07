# LED Test Software — Canonical Feature Inventory

This file is the canonical inventory of **software features and requirements** for the LED development/test system.

It does not replace experimental evidence, protocol records, raw captures, test results, or historical R&D notes. Those remain in their existing records, principally the project Google Drive. GitHub is authoritative for what the software is required to do and its implementation maturity; Drive remains authoritative for the evidence and decisions that justify those requirements.

## Status model

- **Proposed** — candidate capability; not yet accepted as a software requirement.
- **Accepted** — required behaviour, but not yet integrated.
- **In Development** — implementation/prototype work exists but is not yet integrated and verified in the main software path.
- **Implemented** — integrated code path exists; physical/acceptance verification is incomplete.
- **Verified** — integrated behaviour has been exercised against relevant physical evidence and accepted.

A successful experiment does not by itself make a software feature Verified.

## Measurement and evidence

| Feature | Status | Requirement |
| --- | --- | --- |
| Persistent sample/test identity | Accepted | Stable identifiers for sample/specimen, hardware configuration, firmware, test run, measurement and camera/evidence files. |
| Immutable raw evidence | Accepted | Preserve raw instrument/controller evidence separately from interpretations, comparisons and derived results. |
| HPCS spectral acquisition | Verified | Acquire and retain SPD and instrument-reported optical metrics from the HPCS-310P. |
| Derived spectral metrics | Accepted | Retain validated derived metrics with calculation method/version where practical, including CRI/R1–R15/R9 and, when validly implemented, TM-30, SSI, TLCI and TLMF. |
| Test conditions and provenance | Accepted | Record geometry/setup, PSU/electrical state, channel state, warm-up, integration/acquisition settings, timestamps and notes. |
| Evidence attachments | Accepted | Associate setup images, camera files, supplier/EVERFINE reports and operator notes with the relevant experiment/run. |
| Replayable raw captures | Accepted | Permit calculation/software testing from stored raw captures without requiring the physical instrument. |
| EVERFINE-style graphical report | Accepted | Generate a reproducible graphical measurement sheet from stored raw evidence without replacing or altering the raw record. |

## Source stability profiling

| Feature | Status | Requirement |
| --- | --- | --- |
| General 30-second Source Stability Profile | Accepted | Provide an approximately 30-second per-operating-condition characterisation window that measures not only temporal flicker behaviour but the consistency of all optical metrics that can be meaningfully resampled at their appropriate cadence. |
| Timestamped multi-rate acquisition | Accepted | Use a common timeline while allowing each measurement family to run at the fastest sensible cadence supported by its acquisition/integration overhead. Do not require spectral, derived-colour and high-rate temporal measurements to share an artificial common sample rate. |
| Optical consistency traces | Accepted | Where supported, plot and retain time series for output/illuminance, CCT, Duv, chromaticity coordinates, SPD/spectral shape and relevant colour-rendering metrics such as CRI/R values including R9. Derived metrics from one spectrum remain linked to that source spectrum rather than treated as independent observations. |
| Stability statistics | Accepted | For each suitable metric report its time series plus appropriate summary statistics such as mean/median, min/max, standard deviation, range and drift/slope, while retaining the underlying observations. |
| Cross-metric consistency | Accepted | Keep spectral, photometric and temporal observations on the same timestamped run so changes in output, chromaticity, spectrum and flicker can be compared rather than assessed as unrelated tests. |
| Per-condition qualification | Accepted | A Source Stability Profile describes the DUT only at the tested operating state. Material changes in drive level, PWM setting, channel recipe, power state or other relevant conditions require another profile when that state is being qualified. |
| Short stability versus warm-up drift | Accepted | Treat the approximately 30-second profile as short-window repeatability/variability characterisation. Provide a separate longer-duration warm-up/thermal-drift regime for movement toward equilibrium; a stable 30-second window must not be represented as proof of long-term thermal stability. |
| Metric-specific qualification criteria | Accepted | Stability criteria may differ by measurement family and must be explicit/versioned when used for automatic pass/fail. Do not invent one universal threshold across CCT, Duv, output, spectral shape and flicker. |

The flicker temporal-stability gate below is one specialised measurement family within the broader Source Stability Profile. Its independent high-rate waveform slices remain subject to the additional non-contiguity and phase-reconstruction rules described there.

## Flicker and temporal measurement

| Feature | Status | Requirement |
| --- | --- | --- |
| HPCS 3A waveform acquisition | Verified | Acquire a complete exposed 400-sample temporal block and retain the raw bytes, samples and acquisition metadata. |
| HPCS 3C metric acquisition | Verified | Acquire and preserve the four firmware float values. A zero 3C result must never be interpreted as proof of zero physical flicker. |
| Host waveform-derived flicker analysis | In Development | Derive frequency, percent flicker and classical flicker index from viable 3A waveform evidence, with calculation method/version retained. Prototype calculations have been physically validated but are not yet the integrated main-path implementation. |
| Autonomous multi-slice acquisition | In Development | Collect independent timestamped 3A blocks over an extended observation using the continuous acquisition path. A physical prototype has demonstrated repeated unique blocks; the integrated product path must not represent them as a gapless stream. |
| 30-second temporal-stability qualification gate | Accepted | Before treating flicker frequency or waveform shape as representative of a DUT at a given operating condition, run an approximately 30-second multi-slice observation. Record wall-clock observation duration, slice count, sample rate, exposed waveform duration per slice and total actual sampled exposure. |
| Per-condition requalification | Accepted | Temporal stability is a property of the DUT **at the tested operating state**, not permanently of the device. Re-run the gate when a materially different drive level, PWM setting, channel recipe, power state or other relevant operating condition is being qualified. |
| Stability assessment | Accepted | Assess frequency behaviour, modulation depth and waveform-shape repeatability across the observation. Preserve per-slice results and distributions. |
| Versioned stability criteria | Accepted | Pass/fail criteria and thresholds must be explicit and versioned. Do not derive permanent thresholds from the fly-zapper dataset. Calibrate/validate them using controlled known-frequency and known-modulation sources before automatic qualification relies on them. Until then, stability analysis is diagnostic and may be reported as unqualified rather than forced to pass/fail. |
| Phase-composite reconstruction | Accepted | Phase-align independent slices only after stable periodic behaviour is demonstrated. Label the output as a **phase-reconstructed/composite waveform**, never as a continuous recording. |
| Composite/frequency rejection | Accepted | If stability/repeatability is insufficient, do not manufacture a composite waveform and do not promote one Fourier peak to a single representative DUT flicker frequency. Report temporal variability/distributions instead. |
| Truly continuous long waveform | Proposed | Investigate another HPCS rate/window mode or acquisition path if uninterrupted waveform capture beyond the exposed 3A block is required. |

### Temporal-stability evidence informing these requirements

On 2026-10-07, an autonomous BLE capture produced 54 unique 400-sample blocks at 20 kHz over 31.1125 seconds. The exposed blocks contained 1.080 seconds of actual sampled waveform in total, distributed across the wall-clock observation.

The run failed a proposed phase-repeatability test. The approximately 250 Hz component that had been strong and repeatable in earlier captures was weak in this run: its fitted sinusoidal amplitude averaged about 0.090% of mean light level, mean fit correlation was about 0.080, and 250 Hz was the strongest non-DC Fourier bin in 0 of 54 slices. This evidence establishes the need for a temporal-stability gate; it does **not** establish final automated gate thresholds.

## Test-regime engine

| Feature | Status | Requirement |
| --- | --- | --- |
| Saved/versioned regimes | Accepted | Store repeatable qualification procedures such as Pixel Qualification v1. |
| Channel isolation | Accepted | Exercise individual emitter channels independently. |
| Channel combinations | Accepted | Exercise selected/all channel combinations with independent levels. |
| Sweeps and matrices | Accepted | Support level ramps, low-end dimming sweeps, PWM-frequency sweeps and channel recipe matrices. |
| Timing control | Accepted | Support warm-up, settling periods, repetitions and measurements at specified elapsed times. |
| Complete state capture | Accepted | Every regime step records commanded/actual channel state, electrical state, optical/temporal measurement and supporting evidence. |
| Housing qualification | Accepted | Support repeatability, sample removal/replacement, instrument repositioning, geometry, reflector/diffuser and warm-up qualification of the measurement housing. |

## Spectral recipes and comparison

| Feature | Status | Requirement |
| --- | --- | --- |
| Reference → recipe → measurement → comparison | Accepted | Treat source substitution/augmentation as a persistent experimental workflow. |
| Persistent attempted recipes | Accepted | Retain every attempted channel vector and result, not only the selected recipe. |
| Full-spectrum comparison | Accepted | Compare SPD and relevant chromaticity/output/rendering metrics rather than CCT alone. |
| White substitution/augmentation tests | Accepted | Support RGBCA-only, CW+RGBCA, WW/NW+RGBCA and other candidate recipes against measured references. |
| Automatic recipe optimisation | Proposed | Iteratively alter channel levels against a validated objective after camera/material qualification establishes useful objectives. |

## Camera and illumination qualification

| Feature | Status | Requirement |
| --- | --- | --- |
| Illumination-first model | Accepted | Treat the product as a luminaire for camera-captured subjects/environments, not primarily as a directly viewed display. |
| Camera test matrices | Accepted | Support fixed camera setups, frame-rate/shutter combinations, static and movement tests. |
| Material/subject qualification | Accepted | Associate controlled colour targets, skin-tone targets, fabrics, paints, neutrals and saturated materials with illumination tests. |
| Lighting-role geometry | Accepted | Support front/key/fill, overhead/environmental and spill geometries with reproducible distance, angle and optic/diffuser/reflector state. |

## Controller and operation

| Feature | Status | Requirement |
| --- | --- | --- |
| Hardware-independent control API | Accepted | Keep test logic independent of obsolete Teensy assumptions and current/future FPGA implementation details. |
| FPGA/controller integration | Accepted | Set channel state, settle, measure, record actual state and compare through the common control API. |
| GPT-operable deterministic API | Accepted | Expose bounded operations for connection/status, sample identification, channel control, measurement, regimes, comparisons, reports, evidence and historical queries. |
| Conventional/manual control surface | Accepted | Permit setup, diagnostics and manual operation through the same underlying application logic rather than a second measurement implementation. |
| Professional lighting-control compatibility | Accepted | Preserve an adapter path for professional protocols; specific DMX512/RDM, Art-Net and sACN/E1.31 mappings remain deferred. |
| Multi-pixel scale-out | Accepted | Extend qualification/control to multiple pixels only after the single-pixel architecture reaches readiness. |

## Governance

1. This inventory is the canonical home for accepted **software features**.
2. Drive remains the durable authority for R&D evidence, experiments, raw results, hardware records, decisions and handoffs.
3. New experimental findings do not automatically become software features. When a finding implies required software behaviour, amend the relevant feature here while preserving the underlying evidence elsewhere.
4. Existing protocol records, raw captures, historical notes and implementation documentation are additive evidence/context and are not replaced by this file.
5. GitHub issues may track implementation work, but issue closure does not by itself make a feature Verified.
6. **Verified** requires integrated behaviour plus relevant acceptance/physical evidence.
7. Measurement interpretation changes must preserve historical evidence and calculation/version provenance.
8. Do not delete superseded evidence merely because a newer feature definition exists.
