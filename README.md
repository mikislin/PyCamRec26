# PyCamRec

PyCamRec is a Windows-first scientific recorder for high-speed Basler cameras. It preserves owned frame bytes, camera block IDs and timestamps, writes finalized MP4 segments through FFmpeg/NVENC, records experiment metadata, and produces independent acquisition, QC, pixel-fidelity, and experiment-readiness results.

Repository: [github.com/mikislin/PyCamRec26](https://github.com/mikislin/PyCamRec26)

## Current readiness

Version `0.2.0rc1` is **engineering-ready, not yet approved for scientific experiments on the current CXP system**. Built-in profiles describe encoder intent and always say `requires_hardware_validation`; approval lives in a generated config tied to one evidence fingerprint and preview mode.

Latest supplied CXP evidence for Mono8 2464x2064 at 200 fps:

- 30 s, preview off: 199.10 fps, queue 85/1024, exact sampled pixels matched.
- 60 s, preview off: 196.14 fps, queue 586/1024, exact sampled pixels matched, but the queue exceeded the 25% lock margin.
- Preview on: 150-156 fps and 41-78% queue use; real-time QC failed.
- Lossless output: approximately 3.7-4.0 Gbps, or 28-30 GB/min.
- Observed camera temperature: approximately 57-59 C, above the default 40 C warning threshold.

Those sessions did not exercise MP4 rollover, used the older software fingerprint, and had incomplete experiment metadata. They remain useful engineering evidence but cannot lock the revised profile.

Current compact-profile engineering smoke on the attached CXP camera (2026-08-14): 10 seconds produced 2,000/2,000 frames, FFprobe reported 2,000 frames at 2464x2064 and 200 fps, observed host rate was 199.99 fps, queue peaked at 108/1024, and the MP4 was 33,764,988 bytes at 27.02 Mbps. At the same rate, 60 seconds is approximately 202.6 MB. This short, single-segment, metadata-incomplete run validates the size/rate target only; it does not approve the profile for experiments.

## Supported targets and default configs

Top-level `configs/` contains exactly three defaults for each target camera. All produce MP4, use full frame, and default preview off.

| Camera | Lossless | Near-lossless | CV-optimal / smallest default |
|---|---|---|---|
| Basler a2A2448-210cm CXP, 2464x2064 at 200 fps | `pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml`, 4000 Mbps estimate | `pycamrec_basler_a2A2448_cxp_mono8_near_lossless.yaml`, 400 Mbps | `pycamrec_basler_a2A2448_cxp_mono8_cv_optimal.yaml`, 27 Mbps / about 200 MB per minute |
| Basler acA1300-200uc USB, 1280x1024 at the current PFS rate of 80 fps | `pycamrec_basler_acA1300_usb_mono8_lossless.yaml`, 900 Mbps estimate | `pycamrec_basler_acA1300_usb_mono8_near_lossless.yaml`, 400 Mbps | `pycamrec_basler_acA1300_usb_mono8_cv_optimal.yaml`, 100 Mbps |

The USB defaults intentionally use the rate in the supplied PFS and prior evidence. Detect capabilities and revalidate before raising it toward the camera's nominal maximum. “Near-lossless” and “CV-optimal” are lossy; only profiles declaring `pixel_fidelity: lossless...` require and can pass exact source-hash comparison. The 27 Mbps CXP profile is deliberately aggressive: 27 megabits/s is about 202.5 decimal MB (193 MiB) per 60 seconds before small MP4 overhead. Validate detection/tracking accuracy on representative scenes before experiment use.

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

Windows CMD setup:

```bat
set "PY=C:\Users\CodexCore\miniconda3\envs\pycamrec\python.exe"
"%PY%" -m pip install -e .[basler,preview,test]
"%PY%" -m pycamrec devices
"%PY%" -m pycamrec profiles
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
conda install -n pycamrec --force-reinstall "python=3.11" "tk=8.6"
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

Safe stop behavior:

- In the GUI, use **Stop safely**. It creates a stop file, stops acquisition, drains queued frames, closes the current MP4, flushes metadata, and releases the camera.
- In a terminal, press Ctrl-C once and wait for the finalization message.
- A `.part.mp4` file, a finalization event, or a missing session summary makes finalization fail.
- The supplied CXP PFS uses `BslAcquisitionStopMode CompleteExposure` so an in-flight exposure completes during stop.

## Storage and temperature safety

The GUI and preflight use `writer.expected_bitrate_mbps` from the camera config before any generic profile estimate. They show requested output size, free-space margin, and approximate duration at the target rate. Health checks read free space and camera temperature every 10 seconds, not only at segment boundaries.

For CXP lossless, plan for **30 GB/min**:

- 30 s is about 15 GB.
- 60 s is about 30 GB.
- 10 min is about 300 GB.

Do not start a CXP lossless sweep longer than 30 seconds until the expected total has been checked against free space with at least a 20% reserve. Stop and allow cooling when temperature warnings appear; the latest 57-59 C observation is not an approved experiment condition.

## Scientific gates

The report no longer collapses unrelated checks into one `scientific_pass` label.

| Field | Meaning |
|---|---|
| `acquisition_pass` | Session finalized cleanly; frames.csv, segments.csv, expected count, FFprobe count, block IDs, and detected drops agree. |
| `qc_pass` | Acquisition passes, observed rate is at least 98% of expected, and queue stays below 90%. |
| `pixel_pass` | For a lossless claim, decoded gray/raw-mosaic hashes match real camera source hashes. Timing QC cannot change this result. |
| `metadata_pass` | All fixed experiment fields are present. |
| `evidence_ready` | QC, required pixel evidence, and metadata pass; this can be considered for a profile lock. |
| `profile_approval_pass` | The config is locked to the exact current evidence fingerprint and intended preview mode. |
| `experiment_ready` | Evidence is ready and the pre-existing profile approval matches. |

The evidence fingerprint includes camera/model/serial/interface, frame geometry and rate, PFS SHA-256, host and Python, pypylon, FFmpeg version/path, GPU and NVIDIA driver, PyCamRec version, Git commit, and dirty state. A change invalidates a lock before acquisition begins.

Reports for sessions that are still being written return `qc.status: in_progress`; the GUI does not label them frame-integrity failures.

## Hardware validation and profile locking

Validation automatically shortens each case's segment length to at most half the duration, so every successful case must roll over once and then finalize the last MP4. Source hashes capped with `--source-frame-hash-max-frames` are distributed from the beginning through the end of the planned session. Limited decoding also samples across the session and always includes source-hashed frames.

30-second CXP lossless validation in PowerShell:

```powershell
$PY = 'C:\Users\CodexCore\miniconda3\envs\pycamrec\python.exe'
& $PY -m pycamrec validation-sweep configs\pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml --durations 30 --preview both --preview-width 512 --preview-fps 10 --verify-session-pixels --pixel-max-decode-frames 1000 --source-frame-hash-every 10 --source-frame-hash-max-frames 100 --require-complete-metadata
```

The equivalent CMD command:

```bat
set "PY=C:\Users\CodexCore\miniconda3\envs\pycamrec\python.exe"
"%PY%" -m pycamrec validation-sweep configs\pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml --durations 30 --preview both --preview-width 512 --preview-fps 10 --verify-session-pixels --pixel-max-decode-frames 1000 --source-frame-hash-every 10 --source-frame-hash-max-frames 100 --require-complete-metadata
```

The base config must contain complete experiment metadata for that command. A lock recommendation is emitted separately for preview off and preview on. Locking requires every case in that mode to pass acquisition, rollover/finalization, QC, lossless pixel evidence when applicable, metadata, one fingerprint, and queue use below 25%.

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
& $PY -m pycamrec validation-sweep configs\pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml --durations 30 --preview both --dry-run
```

```bat
set "PY=C:\Users\CodexCore\miniconda3\envs\pycamrec\python.exe"
"%PY%" -m compileall -q pycamrec tests
"%PY%" -W error::ResourceWarning -m unittest discover -s tests -v
"%PY%" -m pycamrec validation-sweep configs\pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml --durations 30 --preview both --dry-run
```

PyCamRec is released under the MIT License.
