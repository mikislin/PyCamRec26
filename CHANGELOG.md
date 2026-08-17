# Changelog

## 0.2.0rc2 - 2026-08-14

- Separated GUI profile qualification, current recording setup, and metadata readiness; removed the hard-coded stale CXP preview-failure banner in favor of certificate-derived preview coverage.
- Added metadata JSON load/save, dependency-free DOB and weight timestamp calendar controls, Today/Now actions, typed custom-field shorthand, and hash-backed automatic run-index sequencing.
- Made run indices manually overridable while keeping automatic-next as the default, and added per-check health-duration telemetry for qualification.
- Added naming schema v2 with task/date folders, subject/PND/task/run session names, matching segment filename prefixes, and UUID identity retained in metadata.
- Added non-overwriting `pycamrec report --output` JSON generation and documented custom-config qualification in PowerShell and CMD.
- Added typed metadata v2 with subject weight, postnatal day/P0 semantics, project/protocol/assay identifiers, experimenter/run identifiers, validated custom fields, and a published JSON Schema.
- Added UTC, UUID-backed project/subject/session folder naming plus recursive CSV/JSONL session indexing for portable analysis.
- Bound approvals to both hardware/software and resolved camera/writer/profile/preview fingerprints, with an enforced validated maximum duration and three passing repetitions at that duration.
- Added queue tail-growth, thermal, storage, and combined health gates; critical disk or internal-camera temperature conditions now request a safe stop.
- Added read-only CXP qualification planning with exact repeated-case storage budgets and GUI confirmation before sweeps longer than 30 seconds.
- Added task-quality evidence records as a lock requirement for lossy CXP profiles.
- Prepared all three CXP configs for 30/60-second repeated rollover qualification; lossless is preview-off first, while near-lossless and compact modes qualify preview on and off independently.
- Curated package data explicitly, added a reproducible Conda/Tk environment, expanded Windows CI to Python 3.10-3.12, and added a release-file audit manifest.
- Pinned the verified Windows GUI runtime to Python 3.11.13 / Tk 8.6.14 after Tcl/Tk 8.6.15 failed initialization in the target Conda environment.
- Added the Python 3.10 `tomli` test backport and current Node 24-based GitHub Actions versions for a clean 3.10-3.12 CI matrix.

## 0.2.0rc1 - 2026-08-14

- Fixed integrated setup preview starvation caused by including camera startup latency in cumulative FPS shedding; Mono8 preview now uses downsampled shared memory.
- Retuned the CXP CV-optimal default to 27 Mbps, targeting approximately 200 MB per 60 seconds at full-frame 200 fps.
- Added camera-aware CXP/USB onboarding and six consistent camera-specific MP4 defaults.
- Replaced static validation claims with camera/PFS/GPU/driver/host/software evidence fingerprints and preview-mode-specific profile locks.
- Split acquisition, QC, pixel fidelity, metadata, evidence, approval, and experiment-readiness results.
- Made real-session pixel results independent of timing QC and distributed source/decode samples across the full session.
- Required validation sweeps to exercise MP4 rollover and clean finalization; profile locking now requires queue use below 25%.
- Made recording preview latest-frame-only, strided, early-shedding, and single-threaded for OpenCV work.
- Added in-progress session reporting, periodic temperature/disk health checks, safer disk-duration estimates, and explicit CXP lossless storage warnings.
- Added focused automated tests, MP4 rollover integration coverage, Windows command documentation, package metadata, and runtime-output ignores.

## 0.1.0rc1 - 2026-05-05

- Initial Basler single-camera pre-release with bounded-queue acquisition, segmented FFmpeg output, metadata, preflight, reporting, and GUI support.
