# PyCamRec

PyCamRec is a Windows-first scientific recorder for high-speed Basler cameras. It preserves owned frame bytes, camera block IDs and timestamps, writes finalized MP4 segments through FFmpeg/NVENC, records experiment metadata, and produces independent acquisition, QC, pixel-fidelity, and experiment-readiness results.

Repository: [github.com/mikislin/PyCamRec26](https://github.com/mikislin/PyCamRec26)

## Current readiness

Version `0.2.0rc2` is **engineering-ready, not yet approved for scientific experiments on the current CXP system**. Built-in profiles describe encoder intent and always say `requires_hardware_validation`; approval lives in a generated config tied to hardware/software evidence, resolved profile settings, preview mode, and validated maximum duration.

Latest supplied CXP evidence for Mono8 2464x2064 at 200 fps:

- 30 s, preview off: 199.10 fps, queue 85/1024, exact sampled pixels matched.
- 60 s, preview off: 196.14 fps, queue 586/1024, exact sampled pixels matched, but the queue exceeded the 25% lock margin.
- Preview on: 150-156 fps and 41-78% queue use; real-time QC failed.
- Lossless output: approximately 3.7-4.0 Gbps, or 28-30 GB/min.
- Observed internal device temperature: approximately 57-59 C.

Those sessions did not exercise MP4 rollover, used the older software fingerprint, and had incomplete experiment metadata. They remain useful engineering evidence but cannot lock the revised profile.

Current compact-profile engineering smoke on the attached CXP camera (2026-08-14): 10 seconds produced 2,000/2,000 frames, FFprobe reported 2,000 frames at 2464x2064 and 200 fps, observed host rate was 199.99 fps, queue peaked at 108/1024, and the MP4 was 33,764,988 bytes at 27.02 Mbps. At the same rate, 60 seconds is approximately 202.6 MB. This short, single-segment, metadata-incomplete run validates the size/rate target only; it does not approve the profile for experiments.

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

## Metadata v2 and session naming

The v2 experiment schema uses typed subject and acquisition fields. Weight is a numeric value in grams, postnatal day is a non-negative integer, P0 means the birth date, dates use `YYYY-MM-DD`, and timestamps include a timezone. If DOB and P-day are both present, they must agree on the UTC recording date. The old `animal_id`, `dob`, `test_assay_name`, `experimentator`, and `project_protocol` keys remain readable as v1 aliases but new sessions are written as v2.

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

New sessions use UTC, stable UUIDs, and shallow machine-readable labels:

```text
OUTPUT_ROOT\project-social-vision\subject-m012\2026-08-14\
  20260814T143052231Z__task-open-field__run-001__sid-<uuid>\
```

Weight, P-day, genotype, experimental group, notes, and custom values deliberately stay out of paths. Build a portable analysis index in PowerShell or CMD:

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

The GUI and preflight use `writer.expected_bitrate_mbps` from the camera config before any generic profile estimate. They show requested output size, free-space margin, and approximate duration at the target rate. Health checks read free space and internal camera temperature every 10 seconds, not only at segment boundaries. Crossing the disk reserve or critical internal-temperature threshold requests a safe stop and prevents experiment readiness.

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

Lossy profiles additionally require a predefined scientific task-quality record. Copy `qualification/task_quality_record.template.json`, bind it to the immutable reference dataset and current profile version, record predefined tracking/segmentation/event thresholds and results, and set `status` to `pass` only when all criteria pass. Supply it with `--task-quality-record PATH.json`; its SHA-256 is copied into the approval certificate.

Create a separate approved config only after the summary recommends a lock:

```powershell
& $PY -m pycamrec lock-profile configs\pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml validation_sweeps\YYYYMMDD_HHMMSS\validation_summary.json --preview-mode off --output configs\approved\a2A2448_cxp_mono8_lossless_approved.yaml
```

```bat
"%PY%" -m pycamrec lock-profile configs\pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml validation_sweeps\YYYYMMDD_HHMMSS\validation_summary.json --preview-mode off --output configs\approved\a2A2448_cxp_mono8_lossless_approved.yaml
```

`configs/approved/`, `configs/generated/`, and `validation_sweeps/` are intentionally ignored because they contain hardware/session-specific evidence and local paths.

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

- finalized `segments/segment_*.mp4` files;
- `frames.csv`, including block IDs, camera/host timestamps, queue depth, gap estimates, and optional source hashes;
- `segments.csv` and `events.jsonl`;
- `session.json`, `experiment_metadata.json`, and `analysis_manifest.json`;
- the exact copied PFS;
- `pixel_verification.json` after real-session pixel verification.

Inspect a completed session:

```powershell
& $PY -m pycamrec report D:\PyCamRecSessions\SESSION_FOLDER
& $PY -m pycamrec verify-session-pixels D:\PyCamRecSessions\SESSION_FOLDER --max-decode-frames 1000
```

```bat
"%PY%" -m pycamrec report D:\PyCamRecSessions\SESSION_FOLDER
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
