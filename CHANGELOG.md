# Changelog

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
