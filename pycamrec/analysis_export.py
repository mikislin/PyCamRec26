"""Offline exports for analysis tools."""

from __future__ import annotations

import csv
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AnalysisExportItem:
    source_path: str
    output_path: str
    frames_exported: int
    width: int
    height: int
    fps: float
    mode: str
    encoder: str
    seconds: float
    error: str = ""


@dataclass(frozen=True)
class AnalysisExportReport:
    session_dir: str
    output_dir: str
    pixel_format: str
    items: list[AnalysisExportItem]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["items"] = [asdict(item) for item in self.items]
        return data


def export_session_for_analysis(
    session_dir: Path,
    *,
    output_dir: Path | None = None,
    mode: str = "auto",
    encoder: str = "libx264",
    crf: int = 16,
    qp: int = 16,
    max_frames: int | None = None,
) -> AnalysisExportReport:
    session_dir = session_dir.expanduser().resolve()
    session = _read_json(session_dir / "session.json")
    segments = _read_segments(session_dir / "segments.csv")
    if not segments:
        raise FileNotFoundError(f"No segments found in {session_dir / 'segments.csv'}")

    resolved_camera = _nested(session, ["resolved", "camera"]) or {}
    resolved_writer = _nested(session, ["resolved", "writer"]) or {}
    pixel_format = str(resolved_camera.get("expected_pixel_format") or "")
    width = int(resolved_camera.get("expected_width") or 0)
    height = int(resolved_camera.get("expected_height") or 0)
    fps = float(resolved_camera.get("expected_fps") or 0.0)
    ffmpeg_path = str(resolved_writer.get("ffmpeg_path") or "ffmpeg")
    if width <= 0 or height <= 0 or fps <= 0:
        raise ValueError("Session does not contain resolved camera width, height, and fps.")

    export_mode = _resolved_mode(mode, pixel_format)
    output_dir = output_dir or (session_dir / "analysis_exports")
    output_dir.mkdir(parents=True, exist_ok=True)

    items = []
    for segment in segments:
        source_path = Path(segment["path"])
        if not source_path.is_absolute():
            source_path = session_dir / source_path
        output_path = output_dir / _output_name(source_path, export_mode, encoder)
        items.append(
            _export_segment(
                source_path,
                output_path,
                pixel_format=pixel_format,
                width=width,
                height=height,
                fps=fps,
                mode=export_mode,
                encoder=encoder,
                ffmpeg_path=ffmpeg_path,
                crf=crf,
                qp=qp,
                max_frames=max_frames,
            )
        )

    report = AnalysisExportReport(
        session_dir=str(session_dir),
        output_dir=str(output_dir),
        pixel_format=pixel_format,
        items=items,
    )
    (output_dir / "analysis_export.json").write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return report


def _export_segment(
    source_path: Path,
    output_path: Path,
    *,
    pixel_format: str,
    width: int,
    height: int,
    fps: float,
    mode: str,
    encoder: str,
    ffmpeg_path: str,
    crf: int,
    qp: int,
    max_frames: int | None,
) -> AnalysisExportItem:
    cv2 = _import_cv2()
    capture = cv2.VideoCapture(str(source_path))
    if not capture.isOpened():
        raise RuntimeError(f"Could not open segment for export: {source_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = _ffmpeg_encode_command(
        ffmpeg_path,
        output_path,
        width=width,
        height=height,
        fps=fps,
        encoder=encoder,
        crf=crf,
        qp=qp,
    )
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    started = time.perf_counter()
    frames_exported = 0
    error = ""

    try:
        while max_frames is None or frames_exported < max_frames:
            ok, frame = capture.read()
            if not ok:
                break
            output_frame = _analysis_frame(cv2, frame, pixel_format, mode)
            process.stdin.write(output_frame.tobytes())
            frames_exported += 1
    except Exception as exc:
        error = repr(exc)
    finally:
        try:
            process.stdin.close()
        except Exception:
            pass
        stderr = b""
        try:
            stderr = process.stderr.read() if process.stderr is not None else b""
        except Exception:
            pass
        return_code = process.wait(timeout=300)
        capture.release()

    if return_code != 0 and not error:
        error = stderr.decode("utf-8", errors="replace")[-2000:]
    seconds = time.perf_counter() - started
    return AnalysisExportItem(
        source_path=str(source_path),
        output_path=str(output_path),
        frames_exported=frames_exported,
        width=width,
        height=height,
        fps=fps,
        mode=mode,
        encoder=encoder,
        seconds=seconds,
        error=error,
    )


def _analysis_frame(cv2: Any, frame: Any, pixel_format: str, mode: str) -> Any:
    if mode == "debayer":
        gray = frame[:, :, 0]
        code = getattr(cv2, _bayer_cv2_code_name(pixel_format), None)
        if code is None:
            raise ValueError(f"OpenCV does not support debayer conversion for {pixel_format!r}.")
        return cv2.cvtColor(gray, code)
    if mode == "gray":
        gray = frame[:, :, 0]
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    return frame


def _ffmpeg_encode_command(
    ffmpeg_path: str,
    output_path: Path,
    *,
    width: int,
    height: int,
    fps: float,
    encoder: str,
    crf: int,
    qp: int,
) -> list[str]:
    base = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        f"{width}x{height}",
        "-r",
        _format_fps(fps),
        "-i",
        "-",
        "-an",
    ]
    if encoder == "h264_nvenc":
        args = [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p4",
            "-rc",
            "constqp",
            "-qp",
            str(qp),
            "-bf",
            "0",
            "-pix_fmt",
            "yuv420p",
        ]
    elif encoder == "libx264":
        args = [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ]
    else:
        raise ValueError("encoder must be 'libx264' or 'h264_nvenc'.")
    return base + args + [str(output_path)]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return data


def _read_segments(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _resolved_mode(mode: str, pixel_format: str) -> str:
    mode = mode.strip().lower()
    if mode == "auto":
        return "debayer" if _is_bayer_pixel_format(pixel_format) else "gray"
    if mode in {"debayer", "gray", "passthrough"}:
        if mode == "debayer" and not _is_bayer_pixel_format(pixel_format):
            raise ValueError(f"Cannot debayer non-Bayer pixel format {pixel_format!r}.")
        return mode
    raise ValueError("mode must be 'auto', 'debayer', 'gray', or 'passthrough'.")


def _output_name(source_path: Path, mode: str, encoder: str) -> str:
    encoder_suffix = encoder.replace("_", "-")
    return f"{source_path.stem}_{mode}_{encoder_suffix}.mp4"


def _nested(data: dict[str, Any], path: list[str]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _is_bayer_pixel_format(pixel_format: str) -> bool:
    return _bayer_pattern(pixel_format) is not None


def _bayer_pattern(pixel_format: str) -> str | None:
    normalized = pixel_format.replace("_", "").replace(" ", "").lower()
    for pattern in ("bg", "gb", "gr", "rg"):
        if normalized.startswith(f"bayer{pattern}"):
            return pattern
    return None


def _bayer_cv2_code_name(pixel_format: str) -> str:
    pattern = _bayer_pattern(pixel_format) or "bg"
    return {
        "bg": "COLOR_BayerBG2BGR",
        "gb": "COLOR_BayerGB2BGR",
        "gr": "COLOR_BayerGR2BGR",
        "rg": "COLOR_BayerRG2BGR",
    }[pattern]


def _import_cv2() -> Any:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("Install opencv-python to export analysis videos.") from exc
    return cv2


def _format_fps(fps: float) -> str:
    if abs(fps - round(fps)) < 1e-9:
        return str(int(round(fps)))
    return f"{fps:.6f}".rstrip("0").rstrip(".")
