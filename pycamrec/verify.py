"""Post-recording verification helpers."""

from __future__ import annotations

import csv
import json
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .schemas import PyCamRecConfig


def count_frame_rows(session_dir: Path) -> int:
    frames_path = session_dir / "frames.csv"
    with frames_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return sum(1 for _ in reader)


def ffprobe_video_frames(ffprobe_path: str, video_path: Path) -> int | None:
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_frames",
        "-show_entries",
        "stream=nb_read_frames",
        "-of",
        "json",
        str(video_path),
    ]
    completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
    data: dict[str, Any] = json.loads(completed.stdout)
    streams = data.get("streams") or []
    if not streams:
        return None
    value = streams[0].get("nb_read_frames")
    return int(value) if value is not None else None


@dataclass(frozen=True)
class CodecLosslessReport:
    profile_id: str
    frames: int
    width: int
    height: int
    fps: float
    encoded_path: str
    encoded_bytes: int
    source_hash_count: int
    decoded_hash_count: int
    hashes_match: bool
    first_mismatch_index: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_lossless_codec(
    cfg: PyCamRecConfig,
    output_root: Path,
    frames: int = 100,
) -> CodecLosslessReport:
    if frames <= 0:
        raise ValueError("frames must be positive.")
    output_root.mkdir(parents=True, exist_ok=True)
    test_dir = output_root / "codec_tests" / f"{int(time.time())}_{cfg.recording_profile.id}"
    test_dir.mkdir(parents=True, exist_ok=False)

    source_md5 = test_dir / "source.framemd5"
    decoded_md5 = test_dir / "decoded.framemd5"
    encoded_path = test_dir / f"encoded.{cfg.writer.container}"
    source_filter = (
        f"testsrc2=size={cfg.camera.expected_width}x{cfg.camera.expected_height}:"
        f"rate={_format_fps(cfg.camera.expected_fps)},format=gray"
    )

    _run_ffmpeg(
        [
            cfg.writer.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-f",
            "lavfi",
            "-i",
            source_filter,
            "-frames:v",
            str(frames),
            "-f",
            "framemd5",
            str(source_md5),
        ]
    )
    _run_ffmpeg(
        [
            cfg.writer.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-f",
            "lavfi",
            "-i",
            source_filter,
            "-frames:v",
            str(frames),
            "-an",
            *cfg.writer.output_args,
            str(encoded_path),
        ]
    )
    _run_ffmpeg(
        [
            cfg.writer.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            str(encoded_path),
            "-frames:v",
            str(frames),
            "-vf",
            "format=gray",
            "-f",
            "framemd5",
            str(decoded_md5),
        ]
    )

    source_hashes = _read_framemd5_hashes(source_md5)
    decoded_hashes = _read_framemd5_hashes(decoded_md5)
    first_mismatch = _first_mismatch(source_hashes, decoded_hashes)
    return CodecLosslessReport(
        profile_id=cfg.recording_profile.id,
        frames=frames,
        width=cfg.camera.expected_width,
        height=cfg.camera.expected_height,
        fps=cfg.camera.expected_fps,
        encoded_path=str(encoded_path),
        encoded_bytes=encoded_path.stat().st_size,
        source_hash_count=len(source_hashes),
        decoded_hash_count=len(decoded_hashes),
        hashes_match=first_mismatch is None and len(source_hashes) == len(decoded_hashes),
        first_mismatch_index=first_mismatch,
    )


def _read_framemd5_hashes(path: Path) -> list[str]:
    hashes = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line or not line[0].isdigit():
                continue
            hashes.append(line.rsplit(",", maxsplit=1)[-1].strip())
    return hashes


def _first_mismatch(left: list[str], right: list[str]) -> int | None:
    for index, (left_hash, right_hash) in enumerate(zip(left, right)):
        if left_hash != right_hash:
            return index
    if len(left) != len(right):
        return min(len(left), len(right))
    return None


def _run_ffmpeg(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=300)


def _format_fps(fps: float) -> str:
    if abs(fps - round(fps)) < 1e-9:
        return str(int(round(fps)))
    return f"{fps:.6f}".rstrip("0").rstrip(".")
