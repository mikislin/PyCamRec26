# PyCamRec

PyCamRec is a Windows-first scientific recorder for high-speed Basler cameras. It preserves owned frame bytes, camera block IDs and timestamps, writes finalized MP4 segments through FFmpeg/NVENC, records experiment metadata, and produces independent acquisition, QC, pixel-fidelity, and experiment-readiness results.

Repository: [github.com/mikislin/PyCamRec26](https://github.com/mikislin/PyCamRec26)

## Current readiness

Version `0.2.0rc2` is **engineering-ready, not yet approved for scientific experiments on the current CXP system**. A built-in profile is a candidate definition, not an approval. Approval lives in a separate locked config tied to hardware/software evidence, resolved profile settings, preview mode, and validated maximum duration. The GUI presents profile qualification, current recording setup, and metadata readiness as independent states.

Latest compact CXP engineering evidence for Mono8 2464x2064 at 200 fps, collected on 2026-08-17:

- 12/12 acquisition and QC passes across 30- and 60-second runs, three repeats per duration, preview on and off.
- Exact recorded, expected, and FFprobe frame counts; zero block-ID gaps and zero detected drops.
- Minimum observed rate 199.686 fps (99.843% of requested); worst queue 131/1024 (12.79%).
- Every run rolled over to two MP4 segments and finalized without residual `.part` files.
- 27.009 Mbps weighted output; 60-second MP4 totals were 202.56-202.66 decimal MB.
- Maximum internal camera temperature 58.562 C during the sweep.

Preview-on and preview-off performance both pass the acquisition, metadata, rollover, health, timing, and preferred queue-margin gates. The only remaining scientific gate is real downstream task-quality evidence for the aggressive 27 Mbps compression. PyCamRec must not infer tracking, segmentation, or event accuracy from an acquisition sweep. Any code/configuration or camera/PFS/GPU/driver/host change after that evidence changes a fingerprint and requires a new qualification run before locking.

## Supported targets and default configs

Top-level `configs/` contains exactly three defaults for each target camera. All produce MP4, use full frame, and default preview off.

| Camera | Lossless | Near-lossless | CV-optimal / smallest default |
|---|---|---|---|
| Basler a2A2448-210cm CXP, 2464x2064 at 200 fps | `pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml`, 4000 Mbps estimate | `pycamrec_basler_a2A2448_cxp_mono8_near_lossless.yaml`, 400 Mbps | `pycamrec_basler_a2A2448_cxp_mono8_cv_optimal.yaml`, 27 Mbps / about 200 MB per minute |
| Basler acA1300-200uc USB, 1280x1024 at the current PFS rate of 80 fps | `pycamrec_basler_acA1300_usb_mono8_lossless.yaml`, 900 Mbps estimate | `pycamrec_basler_acA1300_usb_mono8_near_lossless.yaml`, 400 Mbps | `pycamrec_basler_acA1300_usb_mono8_cv_optimal.yaml`, 100 Mbps |

The USB defaults intentionally use the rate in the supplied PFS and prior evidence. Detect capabilities and revalidate before raising it toward the camera's nominal maximum. “Near-lossless” and “CV-optimal” are lossy; only profiles declaring `pixel_fidelity: lossless...` require and can pass exact source-hash comparison. The 27 Mbps CXP profile is deliberately aggressive: 27 megabits/s is about 202.5 decimal MB (193 MiB) per 60 seconds before small MP4 overhead. A passing task-quality record is mandatory before a lossy CXP profile can be locked.

Raw Bayer8 and RGB8/BGR8 are supported through onboarding. Bayer8 can use the one-byte lossless luma path and be debayered offline. RGB/BGR to yuv420p MP4 is color-converted and lossy, so it must not claim exact color fidelity.

## Windows environment

The tested interpreter is:

```text
C:\Users\CodexCore\miniconda3\envs\pycamrec\python.exe
```

PowerShell setup:

```powershell
$PY = 'C:\Users\CodexCore\miniconda3\envs\pycamrec\python.exe'
& $PY -m pip install -e '.[basler,preview,test]'
& $PY -m pycamrec devices
& $PY -m pycamrec profiles
```

Recreate the declared Conda/Tk environment from PowerShell. The file pins the locally verified Python 3.11.13 / Tk 8.6.14 pair:

```powershell
conda env create -f environment.yml
conda activate pycamrec
python -m pycamrec devices
```

Windows CMD setup:

```bat
set "PY=C:\Users\CodexCore\miniconda3\envs\pycamrec\python.exe"
"%PY%" -m pip install -e .[basler,preview,test]
"%PY%" -m pycamrec devices
"%PY%" -m pycamrec profiles
```

Recreate it from CMD:

```bat
conda env create -f environment.yml
call conda activate pycamrec
python -m pycamrec devices
```

The supplied configs point to the local FFmpeg build under `C:\ffmpeg\...\bin`. Change both `writer.ffmpeg_path` and `writer.ffprobe_path` if that installation moves. `pypylon==4.0.0` is the declared Basler extra for the current environment.

Before relying on the desktop GUI, verify that the environment's Tk runtime can create a window:

```powershell
& $PY -c "import tkinter as tk; root = tk.Tk(); root.withdraw(); root.destroy(); print('Tk OK')"
```

```bat
"%PY%" -c "import tkinter as tk; root = tk.Tk(); root.withdraw(); root.destroy(); print('Tk OK')"
```

If this reports that no usable `init.tcl` can be found, repair the Conda environment before using the GUI; this is an interpreter/Tcl installation failure rather than a recorder fallback condition. From PowerShell or CMD:

```text
conda install -n pycamrec --force-reinstall "python=3.11.13" "tk=8.6.14"
```

Re-run the Tk check and then the PyCamRec test suite. Do not substitute an interpreter without the Basler, FFmpeg, and validation dependencies for scientific recording.

## Preflight, GUI, and recording

PowerShell:

```powershell
$PY = 'C:\Users\CodexCore\miniconda3\envs\pycamrec\python.exe'
& $PY -m pycamrec preflight configs\pycamrec_basler_a2A2448_cxp_mono8_cv_optimal.yaml --duration-s 30
& $PY -m pycamrec gui
& $PY -m pycamrec record configs\pycamrec_basler_a2A2448_cxp_mono8_cv_optimal.yaml --duration-s 30
```

Windows CMD:

```bat
set "PY=C:\Users\CodexCore\miniconda3\envs\pycamrec\python.exe"
"%PY%" -m pycamrec preflight configs\pycamrec_basler_a2A2448_cxp_mono8_cv_optimal.yaml --duration-s 30
"%PY%" -m pycamrec gui
"%PY%" -m pycamrec record configs\pycamrec_basler_a2A2448_cxp_mono8_cv_optimal.yaml --duration-s 30
```

Fill every `experiment:` field before a scientific run. For an engineering-only test, append `--allow-unspecified-metadata`; that run can pass acquisition and QC, but it cannot be experiment-ready or lock a profile.

In the GUI, **Selected profile** describes encoder intent. **Profile qualification** says whether a hardware-specific lock certificate exists and covers the selected preview mode/duration. **Current setup** separately reports metadata and preflight readiness. A candidate profile may have excellent past engineering results and still correctly show as unapproved until a passing summary has been converted into and loaded as a locked config.

## Metadata v2 and session naming

The v2 experiment schema uses typed subject and acquisition fields. Weight is a numeric value in grams, postnatal day is a non-negative integer, P0 means the birth date, dates use `YYYY-MM-DD`, and timestamps include a timezone. If DOB and P-day are both present, they must agree on the UTC recording date. The old `animal_id`, `dob`, `test_assay_name`, `experimentator`, and `project_protocol` keys remain readable as v1 aliases but new sessions are written as v2.

The GUI can load a config JSON, a previous `experiment_metadata.json`, or a `session.json`. It imports only user-entered experiment fields, never the old session's automatic hardware/session values. DOB and weight-measurement fields have calendar pickers; **Today** derives P0/PND from DOB, and **Now** records a timezone-aware UTC weight timestamp. **Run index** defaults to **Automatic next**: at recording start PyCamRec finds sessions with otherwise identical metadata and assigns `max(previous run index) + 1`. Editing the number switches to a manual override; **Use next** restores automatic mode. Manual values must be positive integers, and duplicated run numbers should only be used intentionally. A private hash-only state file prevents automatic reuse when a process starts but does not leave a completed session.

```yaml
experiment:
  schema_version: 2
  project:
    project_id: social-vision
    protocol_id: IACUC-2026-014
    assay_id: open-field
  subject:
    subject_id: M012
    species: mouse
    date_of_birth: 2026-07-23
    postnatal_day: 22
    postnatal_day_source: derived_from_date_of_birth
    p0_convention: birth_date_is_p0
    weight_g: 24.3
    weight_measured_utc: 2026-08-14T14:20:00Z
    genotype: wt
    experimental_group: control
    sex: female
  acquisition:
    experimenter_id: ms
    run_index: 1
  custom_fields:
    arena_id: A03
    lighting_lux: 120
  notes: ""
```

Custom keys are normalized to typed records and must use lower-case snake_case. The canonical output is validated against `schemas/experiment_metadata_v2.schema.json`; the GUI accepts a convenient JSON object such as `{"arena_id":"A03","lighting_lux":120}`.

Naming schema v2 keeps UUID identity in JSON metadata while putting the subject, PND, task, and run in human-readable paths:

```text
OUTPUT_ROOT\project-o\subject-4\task-8_2026-08-14\
  20260814T212746807Z__subject-4__P2__task-8__run-002\
    segments\
      20260814T212746807Z__subject-4__P2__task-8__run-002__segment_000001.mp4
      20260814T212746807Z__subject-4__P2__task-8__run-002__segment_000002.mp4
```

Weight, genotype, experimental group, notes, and custom values deliberately stay out of paths. `session_id` remains the globally unique primary key in `session.json`, `experiment_metadata.json`, and the portable session index. Existing naming-v1 recordings remain discoverable because readers find `session.json` recursively and use paths recorded in `segments.csv`.

```powershell
& $PY -m pycamrec index D:\PyCamRecSessions --output D:\PyCamRecSessions\sessions.csv --include-qc
```

```bat
"%PY%" -m pycamrec index D:\PyCamRecSessions --output D:\PyCamRecSessions\sessions.csv --include-qc
```

Safe stop behavior:

- In the GUI, use **Stop safely**. It creates a stop file, stops acquisition, drains queued frames, closes the current MP4, flushes metadata, and releases the camera.
- In a terminal, press Ctrl-C once and wait for the finalization message.
- A `.part.mp4` file, a finalization event, or a missing session summary makes finalization fail.
- The supplied CXP PFS uses `BslAcquisitionStopMode CompleteExposure` so an in-flight exposure completes during stop.

## Storage and temperature safety

The GUI and preflight use `writer.expected_bitrate_mbps` from the camera config before any generic profile estimate. They show requested output size, free-space margin, and approximate duration at the target rate. Health checks read free space and internal camera temperature every 10 seconds, not only at segment boundaries. Each health line and session summary report the last and maximum check duration in milliseconds (`check_ms`, `last_health_check_duration_ms`, and `max_health_check_duration_ms`). Crossing the disk reserve or critical internal-temperature threshold requests a safe stop and prevents experiment readiness.

Keep the 10-second interval for CXP qualification. The check runs in the writer/consumer path while acquisition continues into the bounded queue; it does not pause the camera. A 30-second interval provides less safety and, at the approximately 4 Gbps lossless rate, can allow roughly 15 GB to be written between disk checks instead of about 5 GB. Change the interval only after a qualification run shows a material `check_ms` cost and the full intended configuration is requalified.

For CXP lossless, plan for **30 GB/min**:

- 30 s is about 15 GB.
- 60 s is about 30 GB.
- 10 min is about 300 GB.

Do not start a CXP lossless sweep longer than 30 seconds until the complete repeated-case budget has been checked with at least a 20% reserve. Basler specifies a -10–50°C **housing** operating range for the a2A2448-210cm, while its internal temperature-state documentation lists 76°C warning and 81°C error thresholds. Those are different measurements. The rc2 CXP configs conservatively warn at 70°C internal and request a safe stop at 76°C; the physical installation must also keep the housing inside its specified range. See Basler’s [camera specification](https://www.baslerweb.com/en/shop/a2a2448-210cm/) and [temperature-state documentation](https://docs.baslerweb.com/temperature-state).

## Scientific gates

The report no longer collapses unrelated checks into one `scientific_pass` label.

| Field | Meaning |
|---|---|
| `acquisition_pass` | Session finalized cleanly; frames.csv, segments.csv, expected count, FFprobe count, block IDs, and detected drops agree. |
| `qc_pass` | Acquisition passes, observed rate is at least 98% of expected, and queue stays below 90%. |
| `queue_margin_pass` | Queue remains below 25% of capacity, as required for profile qualification. |
| `queue_growth_pass` | Backlog growth over the final 20% of the run is no more than 0.5 frames/s. |
| `pixel_pass` | For a lossless claim, decoded gray/raw-mosaic hashes match real camera source hashes. Timing QC cannot change this result. |
| `metadata_pass` | Required typed experiment fields are present and semantically valid. |
| `thermal_pass` / `storage_pass` | Periodic temperature and free-space evidence remains below/above its critical thresholds. |
| `evidence_ready` | QC, health, required pixel evidence, and metadata pass; this can be considered for a profile lock. |
| `profile_approval_pass` | Hardware/software fingerprint, resolved settings fingerprint, preview mode, and duration match an approval certificate. |
| `experiment_ready` | Evidence is ready and the pre-existing profile approval matches. |

The evidence fingerprint includes camera/model/serial/interface, frame geometry and rate, PFS SHA-256, host and Python, pypylon, FFmpeg version/path, GPU and NVIDIA driver, PyCamRec version, Git commit, and dirty state. A separate resolved-profile fingerprint covers camera, encoder/writer arguments, profile version, and performance-relevant preview settings. A change to either fingerprint invalidates a lock before acquisition begins. Requests longer than the validated maximum duration are also rejected.

Reports for sessions that are still being written return `qc.status: in_progress`; the GUI does not label them frame-integrity failures.

## Hardware validation and profile locking

Validation automatically shortens each case's segment length to at most half the duration, so every successful case must roll over once and then finalize the last MP4. Source hashes capped with `--source-frame-hash-max-frames` are distributed from the beginning through the end of the planned session. Limited decoding also samples across the session and always includes source-hashed frames. A lock requires three passing repetitions at the maximum duration, health evidence, queue below 25%, stable end-of-run backlog, one hardware fingerprint, one resolved profile fingerprint, complete metadata, and exact sampled pixels for lossless profiles.

GUI qualification workflow:

1. Complete **Recording metadata**; qualification evidence with unspecified metadata cannot be locked.
2. On **Profile qualification**, select the candidate YAML and matching camera PFS, then use **Detect camera caps**.
3. Set the intended durations, preview mode, required repetitions, and rollover segment length. Preview on and off are separate approvals.
4. For a lossless profile, configure source/pixel hashing. For a lossy profile, an existing task-quality JSON is optional; the sweep creates or updates a working record automatically.
5. Use **Run qualification sweep**. The sweep writes `validation_summary.json`, `task_quality_record.json`, and `profile_status.json` in one timestamped folder. Exit code 0 means acquisition completed, not that the lossy profile is approved.
6. Use **Finalize & create both approved YAMLs...**. For a lossy profile, select an immutable reference dataset/archive/manifest and enter the three independently measured task metrics. PyCamRec hashes the artifact and computes the result from the thresholds. When every gate passes, it updates the summary/status and creates both preview-off and preview-on YAMLs in one action.
7. Run **Preflight** from **Setup & Record**. Startup still verifies the live camera/PFS/GPU/driver/host/software fingerprint.

Print the complete cases, exact command, and disk budget without recording:

```powershell
$PY = 'C:\Users\CodexCore\miniconda3\envs\pycamrec\python.exe'
& $PY -m pycamrec qualification-plan configs\pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml
```

```bat
set "PY=C:\Users\CodexCore\miniconda3\envs\pycamrec\python.exe"
"%PY%" -m pycamrec qualification-plan configs\pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml
```

The rc2 lossless plan contains three 30-second and three 60-second preview-off runs: approximately 135 GB of output before reserve. Run it only after filling metadata, confirming storage/cooling, and reviewing the printed command. PowerShell:

```powershell
& $PY -m pycamrec validation-sweep configs\pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml --durations 30,60 --preview off --repeats 3 --required-passing-repeats 3 --verify-session-pixels --pixel-max-decode-frames 1000 --source-frame-hash-every 10 --source-frame-hash-max-frames 100 --require-complete-metadata
```

CMD:

```bat
"%PY%" -m pycamrec validation-sweep configs\pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml --durations 30,60 --preview off --repeats 3 --required-passing-repeats 3 --verify-session-pixels --pixel-max-decode-frames 1000 --source-frame-hash-every 10 --source-frame-hash-max-frames 100 --require-complete-metadata
```

The base config must contain complete experiment metadata. A lock recommendation is emitted separately for preview off and preview on. Lossless is initially qualified preview-off only. Near-lossless and compact CXP configs plan both modes independently.

Lossy profiles additionally require predefined scientific task-quality criteria. The sweep creates `task_quality_record.json` beside its summary (or updates the working JSON supplied with `--task-quality-record`) and binds it to the profile, sweep counts, preview modes, and hardware fingerprint. It remains `pending` until real task metrics and an immutable reference-artifact hash are present. Do not edit `status`: the app calculates `pending`, `fail`, or `pass` from every numeric `*_min`/`*_max` criterion.

Finalize a completed lossy sweep without rerunning the camera. Substitute the real evaluation artifact and measured results; the example numbers are not defaults or claims:

```powershell
$SUMMARY = 'validation_sweeps\YYYYMMDD_HHMMSS\validation_summary.json'
$REFERENCE = 'D:\TaskValidation\cxp_27m_reference_v1.zip'
& $PY -m pycamrec finalize-qualification configs\pycamrec_basler_a2A2448_cxp_mono8_cv_optimal.yaml $SUMMARY --reference-dataset $REFERENCE --tracking-median-error-px 0.80 --segmentation-iou 0.96 --event-f1 0.97 --approved-dir configs\approved
```

```bat
set "SUMMARY=validation_sweeps\YYYYMMDD_HHMMSS\validation_summary.json"
set "REFERENCE=D:\TaskValidation\cxp_27m_reference_v1.zip"
"%PY%" -m pycamrec finalize-qualification configs\pycamrec_basler_a2A2448_cxp_mono8_cv_optimal.yaml "%SUMMARY%" --reference-dataset "%REFERENCE%" --tracking-median-error-px 0.80 --segmentation-iou 0.96 --event-f1 0.97 --approved-dir configs\approved
```

The command reads the sweep-local task record by default, updates `validation_summary.json` while preserving `validation_summary.acquisition.json`, writes `profile_status.json`, and creates `*_preview_off_approved.yaml` plus `*_preview_on_approved.yaml`. It refuses partial metrics, failed thresholds, missing preview-mode evidence, profile mismatches, or existing destination files.

Create a separate approved config only after the summary recommends a lock:

```powershell
& $PY -m pycamrec lock-profile configs\pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml validation_sweeps\YYYYMMDD_HHMMSS\validation_summary.json --preview-mode off --output configs\approved\a2A2448_cxp_mono8_lossless_approved.yaml
```

```bat
"%PY%" -m pycamrec lock-profile configs\pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml validation_sweeps\YYYYMMDD_HHMMSS\validation_summary.json --preview-mode off --output configs\approved\a2A2448_cxp_mono8_lossless_approved.yaml
```

`configs/approved/`, `configs/generated/`, and `validation_sweeps/` are intentionally ignored because they contain hardware/session-specific evidence and local paths.

### Validate a custom recording config and generate its report

A custom GUI config currently inherits a registered `profile` ID so PyCamRec knows whether it claims losslessness and which scientific gate applies. Copy the closest camera/fidelity candidate, give the copied YAML a clear filename, and change only reviewed camera/writer/preview settings. Do not retain a `lossless` profile ID after introducing a lossy pixel conversion. Keep `approval.status: requires_hardware_validation` until qualification passes.

PowerShell example:

```powershell
$PY = 'C:\Users\CodexCore\miniconda3\envs\pycamrec\python.exe'
$CUSTOM = 'configs\my_a2A2448_cxp_profile.yaml'
Copy-Item configs\pycamrec_basler_a2A2448_cxp_mono8_cv_optimal.yaml $CUSTOM
# Edit $CUSTOM: writer/output_args, expected_bitrate_mbps, qualification policy, and complete experiment metadata.
& $PY -m pycamrec preflight $CUSTOM --duration-s 30
& $PY -m pycamrec qualification-plan $CUSTOM
& $PY -m pycamrec validation-sweep $CUSTOM --durations 30,60 --preview both --repeats 3 --required-passing-repeats 3 --require-complete-metadata
$SUMMARY = 'validation_sweeps\YYYYMMDD_HHMMSS\validation_summary.json'
& $PY -m pycamrec finalize-qualification $CUSTOM $SUMMARY --reference-dataset D:\TaskValidation\custom_profile_reference.zip --tracking-median-error-px 0.80 --segmentation-iou 0.96 --event-f1 0.97 --approved-dir configs\approved
```

CMD example:

```bat
set "PY=C:\Users\CodexCore\miniconda3\envs\pycamrec\python.exe"
set "CUSTOM=configs\my_a2A2448_cxp_profile.yaml"
copy configs\pycamrec_basler_a2A2448_cxp_mono8_cv_optimal.yaml "%CUSTOM%"
rem Edit %CUSTOM%: writer/output_args, expected_bitrate_mbps, qualification policy, and complete experiment metadata.
"%PY%" -m pycamrec preflight "%CUSTOM%" --duration-s 30
"%PY%" -m pycamrec qualification-plan "%CUSTOM%"
"%PY%" -m pycamrec validation-sweep "%CUSTOM%" --durations 30,60 --preview both --repeats 3 --required-passing-repeats 3 --require-complete-metadata
set "SUMMARY=validation_sweeps\YYYYMMDD_HHMMSS\validation_summary.json"
"%PY%" -m pycamrec finalize-qualification "%CUSTOM%" "%SUMMARY%" --reference-dataset D:\TaskValidation\custom_profile_reference.zip --tracking-median-error-px 0.80 --segmentation-iou 0.96 --event-f1 0.97 --approved-dir configs\approved
```

For a lossless custom config, add `--verify-session-pixels --pixel-max-decode-frames 1000 --source-frame-hash-every 10 --source-frame-hash-max-frames 100` to the sweep and omit task-metric arguments. `finalize-qualification` creates two configs only when the sweep contains passing evidence for both preview modes; use the lower-level `lock-profile` command when intentionally qualifying just one mode. Select the resulting locked config in the GUI; selecting the original custom candidate does not carry approval forward.

After any completed run, create a persistent, non-overwriting JSON report:

```powershell
$SESSION = 'D:\PyCamRecSessions\project-o\subject-4\task-8_2026-08-14\SESSION_FOLDER'
& $PY -m pycamrec report $SESSION --output "$SESSION\scientific_report.json"
```

```bat
set "SESSION=D:\PyCamRecSessions\project-o\subject-4\task-8_2026-08-14\SESSION_FOLDER"
"%PY%" -m pycamrec report "%SESSION%" --output "%SESSION%\scientific_report.json"
```

The report command refuses to overwrite an existing report. Use a new filename when intentionally regenerating after a software change.

## Camera onboarding and color modes

Detect the attached camera and generate a reviewable candidate. CXP Mono8 now selects the CXP lossless profile and 4000 Mbps estimate, never the USB profile.

PowerShell Bayer example for the USB camera:

```powershell
$PY = 'C:\Users\CodexCore\miniconda3\envs\pycamrec\python.exe'
& $PY -m pycamrec camera-capabilities --serial 24188001
& $PY -m pycamrec make-camera-config configs\pycamrec_basler_acA1300_usb_mono8_lossless.yaml --camera-make basler_usb --serial 24188001 --pfs-path acA1300-200uc_24188001.pfs --pixel-format BayerBG8 --expected-fps 80 --width 1280 --height 1024 --output configs\generated\acA1300_bayer8_candidate.yaml
```

CMD:

```bat
set "PY=C:\Users\CodexCore\miniconda3\envs\pycamrec\python.exe"
"%PY%" -m pycamrec camera-capabilities --serial 24188001
"%PY%" -m pycamrec make-camera-config configs\pycamrec_basler_acA1300_usb_mono8_lossless.yaml --camera-make basler_usb --serial 24188001 --pfs-path acA1300-200uc_24188001.pfs --pixel-format BayerBG8 --expected-fps 80 --width 1280 --height 1024 --output configs\generated\acA1300_bayer8_candidate.yaml
```

The GUI's Mono8 setup preview uses downsampled shared memory; Bayer/RGB setup preview keeps raw pixels so the GUI can render color correctly. Setup preview is never hidden by recording-oriented FPS shedding. Recording preview uses a latest-frame-only, strided shared-memory image, one OpenCV thread, a 10 fps default, and rolling-rate/queue-based load shedding. Preview evidence is still mode-specific.

The 2026-08-14 attached-camera GUI smoke rendered 83 setup-preview frames without preview errors, compared with only 2-6 frames before the cumulative-startup-rate fix.

## Session artifacts

Each session contains:

- finalized `segments/<session-name>__segment_*.mp4` files;
- `frames.csv`, including block IDs, camera/host timestamps, queue depth, gap estimates, and optional source hashes;
- `segments.csv` and `events.jsonl`;
- `session.json`, `experiment_metadata.json`, and `analysis_manifest.json`;
- the exact copied PFS;
- `pixel_verification.json` after real-session pixel verification.

Inspect a completed session:

```powershell
& $PY -m pycamrec report D:\PyCamRecSessions\SESSION_FOLDER --output D:\PyCamRecSessions\SESSION_FOLDER\scientific_report.json
& $PY -m pycamrec verify-session-pixels D:\PyCamRecSessions\SESSION_FOLDER --max-decode-frames 1000
```

```bat
"%PY%" -m pycamrec report D:\PyCamRecSessions\SESSION_FOLDER --output D:\PyCamRecSessions\SESSION_FOLDER\scientific_report.json
"%PY%" -m pycamrec verify-session-pixels D:\PyCamRecSessions\SESSION_FOLDER --max-decode-frames 1000
```

## Development checks

The core suite does not require a camera. It includes MP4 rollover/finalization through the installed FFmpeg when available.

```powershell
$PY = 'C:\Users\CodexCore\miniconda3\envs\pycamrec\python.exe'
& $PY -m compileall -q pycamrec tests
& $PY -W error::ResourceWarning -m unittest discover -s tests -v
& $PY scripts\release_audit.py
& $PY -m pycamrec validation-sweep configs\pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml --durations 30 --preview both --dry-run
```

```bat
set "PY=C:\Users\CodexCore\miniconda3\envs\pycamrec\python.exe"
"%PY%" -m compileall -q pycamrec tests
"%PY%" -W error::ResourceWarning -m unittest discover -s tests -v
"%PY%" scripts\release_audit.py
"%PY%" -m pycamrec validation-sweep configs\pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml --durations 30 --preview both --dry-run
```

`release-files.txt` is the exact 0.2.0rc2 public file list. The release audit compares it with every tracked or unignored Git candidate and rejects release-surface drift, recordings, runtime/generated folders, secret-bearing filenames, and unexpectedly large files. Local sessions, sweeps, generated/approved configs, GUI state, build output, and legacy sandbox material stay ignored; they are not part of the public repository.

PyCamRec is released under the MIT License.
