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

```powershell
conda env create --name pycamrec --file environment.yml

conda activate pycamrec

python -m pip install --editable ".[basler,preview,test]"
```

Verify the installation
```powershell
python --version
python -m pycamrec --help
python -m pycamrec profiles
python -m pytest -q
python scripts\release_audit.py
```

Launch the Windows desktop app with:

```powershell
python -m pycamrec gui
```

The current user-facing configs are profile based. Provide the duration from the command line:

```powershell
python -m pycamrec preflight configs/pycamrec_basler_a2A2448_profile_long_lossy.yaml --60
python -m pycamrec record configs/pycamrec_basler_a2A2448_profile_long_lossy.yaml --60
python -m pycamrec report C:\PyCamRecSessions\<session_folder>
```


Live preview is optional and deliberately best-effort. Recording frames always go to the writer queue first; preview receives only the latest sampled frame and silently drops preview updates if display cannot keep up. For the GUI sink, the recorder publishes a sampled raw Mono8 frame and per-frame status into shared memory, while the GUI process handles preview downsampling/display.

```powershell
pip install opencv-python
python -m pycamrec record configs/pycamrec_basler_a2A2448_profile_near_lossless.yaml --60 --preview --allow-unspecified-metadata
python -m pycamrec record configs/pycamrec_basler_a2A2448_profile_near_lossless.yaml --60 --preview --preview-width 800 --preview-fps 30
```

Close only the standalone OpenCV preview window with `q` or Esc. Stop recording from the terminal with Ctrl-C, or from the GUI with Stop safely, so PyCamRec can drain the writer queue, close the segment, flush metadata, and release the camera.

During recordings PyCamRec prints a health line after each segment closes. It records free disk space and camera temperature in `events.jsonl`, warning when the profile's free-space guard is crossed or camera temperature is above 70 C.

To interrupt a long run, press Ctrl-C once and wait for PyCamRec to print that the session was finalized and the camera was released. It will stop acquisition, drain queued frames, close the current video segment, flush metadata, and write the session summary.


# TODO
1. Fix delay between starting video recording and camera initialization
2. Add LUT options (inferno, spectrum...) for preview to help with lighting calibration
3. Add room temperature to the metadata
4. data transfer and backup
5. archiving recordings with metadata to the NWB format for publications
