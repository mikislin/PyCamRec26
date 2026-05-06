# PyCamRec

PyCamRec is the focused Basler acquisition layer started from the CamPy (https://github.com/ksseverson57/campy).

Current implementation status:
- Basler-only, single-camera recorder.
- Loads and validates the supplied `.pfs`.
- Validates serial, frame size, pixel format, and frame rate.
- Copies each frame into owned bytes before releasing the pylon grab result.
- Uses a bounded queue and aborts if the writer cannot keep up.
- Writes continuous `frames.csv` metadata during acquisition.
- Writes segmented FFmpeg output into `segments/`.
- Writes `session.json`, copied `.pfs`, `segments.csv`, and `events.jsonl`.

Run once a Python environment with `pypylon`, `numpy`, and `PyYAML` is active.
For the pylon Viewer 7.5, use `pypylon==4.0.0`; that release matches pylon 7.5 on Windows/Linux.

```powershell
pip uninstall -y pypylon
pip install pypylon==4.0.0
```

The current user-facing configs are profile based. Provide the duration from the command line:

```powershell
python -m pycamrec preflight configs/pycamrec_basler_a2A2448_profile_long_lossy.yaml --60
python -m pycamrec record configs/pycamrec_basler_a2A2448_profile_long_lossy.yaml --60
python -m pycamrec report D:\PyCamRecSessions\<session_folder>
```

Before scientific recording, fill the `experiment:` block in the selected profile config. Fields default to `UNSPECIFIED` on purpose; PyCamRec never reuses previous animal/session values. Recording refuses incomplete metadata by default; for engineering-only tests, pass `--allow-unspecified-metadata`. Preflight and report also flag missing fields. `--fail-on-warning` is stricter than normal scientific mode and will also stop on expected warnings such as the PFS chunk-mode notice.

```yaml
experiment:
  animal_id: "UNSPECIFIED"
  dob: "UNSPECIFIED"
  test_assay_name: "UNSPECIFIED"
  genotype: "UNSPECIFIED"
  experimental_group: "UNSPECIFIED"
  sex: "UNSPECIFIED"
  experimentator: "UNSPECIFIED"
  project_protocol: "UNSPECIFIED"
  camera_profile_path: "UNSPECIFIED"
  notes: ""
```

Each session writes both machine-readable `session.json` and human-readable `experiment_metadata.json`. The experiment metadata file includes the fixed fields plus automatic capture of date/time, computer name, camera serial, CXP interface card, PFS hash, camera settings, recording profile version, PyCamRec version/git commit when available, disk path/free space, and camera temperature.

The validated long-recording ladder uses the long lossy profile:

```powershell
python -m pycamrec record configs/pycamrec_basler_a2A2448_profile_long_lossy.yaml --600
python -m pycamrec record configs/pycamrec_basler_a2A2448_profile_long_lossy.yaml --1800
python -m pycamrec record configs/pycamrec_basler_a2A2448_profile_long_lossy.yaml --7200
```

Built-in recording profiles can be listed with:

```powershell
python -m pycamrec profiles
```

Launch the Windows desktop app with:

```powershell
python -m pycamrec gui
```

The desktop app has tabs for device status, recording setup, experiment metadata, live preview controls, recording monitor output, and report browsing. It writes a run-specific config into `.pycamrec_gui/` so the three user-facing profile YAMLs stay clean and continue to default metadata fields to `UNSPECIFIED`. The Stop safely button creates a run-specific stop file instead of sending a console interrupt, so PyCamRec can drain frames, close the segment, write metadata, and release the camera.

The GUI uses a three-panel layout: left-side settings/report tabs, a preallocated right-side live preview panel, and a bottom command-output log. GUI preview is integrated into that panel through a downsampled shared-memory grayscale frame buffer plus a tiny `.pycamrec_gui/latest_preview.json` status sidecar; it does not open a separate OpenCV window. The GUI auto-fits that image when the window is maximized or resized. The default GUI preview target is 512 pixels wide at 20 fps, and preview updates are dropped under writer queue pressure. Use wider preview, such as 800 pixels, for setup/alignment checks rather than final timing validation.

Top-level `configs/` contains only the three user-facing profile configs:

```powershell
python -m pycamrec preflight configs/pycamrec_basler_a2A2448_profile_long_lossy.yaml --60
python -m pycamrec preflight configs/pycamrec_basler_a2A2448_profile_near_lossless.yaml --600
python -m pycamrec preflight configs/pycamrec_basler_a2A2448_profile_lossless.yaml --30
```

Live preview is optional and deliberately best-effort. Recording frames always go to the writer queue first; preview receives only the latest sampled frame and silently drops preview updates if display cannot keep up. For the GUI sink, the recorder publishes a sampled raw Mono8 frame and per-frame status into shared memory, while the GUI process handles preview downsampling/display. A tiny JSON sidecar is used only to advertise the shared-memory buffer name and shape to the GUI.

```powershell
pip install opencv-python
python -m pycamrec record configs/pycamrec_basler_a2A2448_profile_near_lossless.yaml --60 --preview --allow-unspecified-metadata
python -m pycamrec record configs/pycamrec_basler_a2A2448_profile_near_lossless.yaml --60 --preview --preview-width 800 --preview-fps 30
```

Close only the standalone OpenCV preview window with `q` or Esc. Stop recording from the terminal with Ctrl-C, or from the GUI with Stop safely, so PyCamRec can drain the writer queue, close the segment, flush metadata, and release the camera.

During recordings PyCamRec prints a health line after each segment closes. It records free disk space and camera temperature in `events.jsonl`, warning when the profile's free-space guard is crossed or camera temperature is above 40 C.

To interrupt a long run, press Ctrl-C once and wait for PyCamRec to print that the session was finalized and the camera was released. It will stop acquisition, drain queued frames, close the current video segment, flush metadata, and write the session summary.

