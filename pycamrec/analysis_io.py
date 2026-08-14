"""Convenience helpers for reading PyCamRec videos in analysis code."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .pixel_formats import channel_semantics


@dataclass(frozen=True)
class VideoReadInspection:
    path: str
    mode: str
    ok: bool
    frame_shape: tuple[int, ...] | None
    dtype: str
    source_pixel_format: str
    channel_semantics: str
    equal_rgb_channels: bool | None
    recommendation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def inspect_video_read(path: Path, *, mode: str = "auto", frame_index: int = 0) -> VideoReadInspection:
    cv2 = _import_cv2()
    try:
        cv2.setLogLevel(0)
    except Exception:
        pass
    path = path.expanduser().resolve()
    source_pixel_format = _session_pixel_format_for_segment(path)
    semantics = channel_semantics(source_pixel_format) if source_pixel_format else ""
    resolved_mode = _resolve_mode(mode, semantics)

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        return VideoReadInspection(
            path=str(path),
            mode=resolved_mode,
            ok=False,
            frame_shape=None,
            dtype="",
            source_pixel_format=source_pixel_format,
            channel_semantics=semantics,
            equal_rgb_channels=None,
            recommendation="Could not open video with OpenCV.",
        )
    if resolved_mode in {"gray", "raw"}:
        capture.set(cv2.CAP_PROP_CONVERT_RGB, 0)
    if frame_index:
        capture.set(cv2.CAP_PROP_POS_FRAMES, int(frame_index))
    ok, frame = capture.read()
    capture.release()

    equal_channels = None
    shape = None
    dtype = ""
    if ok and frame is not None:
        shape = tuple(int(item) for item in frame.shape)
        dtype = str(frame.dtype)
        if len(frame.shape) == 3 and frame.shape[2] >= 3:
            equal_channels = bool(((frame[:, :, 0] == frame[:, :, 1]).all()) and ((frame[:, :, 1] == frame[:, :, 2]).all()))

    return VideoReadInspection(
        path=str(path),
        mode=resolved_mode,
        ok=bool(ok),
        frame_shape=shape,
        dtype=dtype,
        source_pixel_format=source_pixel_format,
        channel_semantics=semantics,
        equal_rgb_channels=equal_channels,
        recommendation=_recommendation(resolved_mode, semantics, equal_channels),
    )


def _resolve_mode(mode: str, semantics: str) -> str:
    mode = mode.strip().lower()
    if mode != "auto":
        if mode not in {"gray", "color", "raw"}:
            raise ValueError("mode must be 'auto', 'gray', 'color', or 'raw'.")
        return mode
    if semantics == "mono_luma" or semantics.startswith("raw_bayer"):
        return "gray"
    return "color"


def _session_pixel_format_for_segment(path: Path) -> str:
    for parent in (path.parent, path.parent.parent, path.parent.parent.parent):
        session_path = parent / "session.json"
        if session_path.is_file():
            try:
                session = json.loads(session_path.read_text(encoding="utf-8"))
            except Exception:
                return ""
            camera = ((session.get("resolved") or {}).get("camera") or {})
            return str(camera.get("expected_pixel_format") or "")
    return ""


def _recommendation(mode: str, semantics: str, equal_channels: bool | None) -> str:
    if semantics == "mono_luma":
        return "Use the returned 2D frame when mode=gray; RGB displays with identical channels are expected for Mono8 videos."
    if semantics.startswith("raw_bayer"):
        return "This is a raw Bayer mosaic. Use export-analysis --mode debayer or a controlled debayer step for color."
    if semantics in {"rgb", "bgr"}:
        return "This video came from color camera bytes. Decode in color mode for analysis; H.264 MP4 still stores color as YUV internally."
    if equal_channels:
        return "The reader exposed three identical channels; use one channel or rerun with mode=gray."
    return "No PyCamRec session metadata was found for this video; inspect session.json or analysis_manifest.json if available."


def _import_cv2() -> Any:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("Install opencv-python to inspect video reads.") from exc
    return cv2
