"""Continuous metadata logging for PyCamRec."""

from __future__ import annotations

import csv
import json
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __release_stage__, __version__
from .preflight import PreflightReport, sha256_file
from .schemas import PyCamRecConfig


@dataclass(frozen=True)
class FrameMetadata:
    frame_index: int
    camera_block_id: int | None
    camera_timestamp_raw: int | None
    camera_timestamp_ns: int | None
    host_receive_perf_counter_ns: int
    host_receive_utc_ns: int
    payload_size_bytes: int
    queue_depth_after_enqueue: int
    writer_segment_id: int
    dropped_before_frame: int
    status: str = "grabbed"
    error: str = ""


@dataclass(frozen=True)
class SegmentMetadata:
    segment_id: int
    path: str
    first_frame_index: int
    last_frame_index: int
    frame_count: int
    started_utc: str
    finished_utc: str
    size_bytes: int
    sha256: str


class MetadataWriter:
    """Append-only metadata writer.

    Frame metadata is flushed periodically so a power loss loses at most a small
    tail of the timing record.
    """

    def __init__(self, cfg: PyCamRecConfig, preflight: PreflightReport, device_info: dict[str, Any]):
        self.cfg = cfg
        self.preflight = preflight
        self.device_info = device_info
        self.session_dir = self._make_session_dir()
        self.segments_dir = self.session_dir / "segments"
        self.segments_dir.mkdir(parents=True, exist_ok=False)

        self.frames_path = self.session_dir / "frames.csv"
        self.segments_path = self.session_dir / "segments.csv"
        self.events_path = self.session_dir / "events.jsonl"
        self.experiment_metadata_path = self.session_dir / "experiment_metadata.json"
        self._frame_count_since_flush = 0
        self._closed = False

        self._copy_inputs()
        self._write_session_json()
        self._write_experiment_metadata_json()

        self._frames_file = self.frames_path.open("w", newline="", encoding="utf-8")
        self._frames_csv = csv.DictWriter(
            self._frames_file,
            fieldnames=[field.name for field in FrameMetadata.__dataclass_fields__.values()],
        )
        self._frames_csv.writeheader()

        self._segments_file = self.segments_path.open("w", newline="", encoding="utf-8")
        self._segments_csv = csv.DictWriter(
            self._segments_file,
            fieldnames=[field.name for field in SegmentMetadata.__dataclass_fields__.values()],
        )
        self._segments_csv.writeheader()

        self.log_event("session_started", {"session_dir": str(self.session_dir)})

    def append_frame(self, frame: FrameMetadata) -> None:
        self._frames_csv.writerow(asdict(frame))
        self._frame_count_since_flush += 1
        if self._frame_count_since_flush >= self.cfg.metadata.flush_every_frames:
            self.flush()

    def append_segment(self, segment: SegmentMetadata) -> None:
        self._segments_csv.writerow(asdict(segment))
        self._segments_file.flush()

    def log_event(self, kind: str, payload: dict[str, Any]) -> None:
        item = {
            "utc": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "payload": payload,
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, sort_keys=True, default=str) + "\n")

    def flush(self) -> None:
        self._frames_file.flush()
        self._segments_file.flush()
        self._frame_count_since_flush = 0

    def close(self, summary: dict[str, Any] | None = None) -> None:
        if self._closed:
            return
        if summary:
            self.log_event("session_summary", summary)
            self._write_experiment_metadata_json(summary=summary)
        self.flush()
        self._frames_file.close()
        self._segments_file.close()
        self._closed = True

    def _make_session_dir(self) -> Path:
        root = self.cfg.session.output_root
        root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in self.cfg.session.name)
        session_dir = root / f"{timestamp}_{safe_name}_{self.cfg.camera.serial}"
        if session_dir.exists():
            suffix = time.perf_counter_ns()
            session_dir = root / f"{timestamp}_{safe_name}_{self.cfg.camera.serial}_{suffix}"
        session_dir.mkdir(parents=False, exist_ok=False)
        return session_dir

    def _copy_inputs(self) -> None:
        shutil.copy2(self.cfg.camera.pfs_path, self.session_dir / self.cfg.camera.pfs_path.name)

    def _write_session_json(self) -> None:
        session_doc = {
            "schema_version": 1,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "host": socket.gethostname(),
            "config": _jsonable(self.cfg.raw),
            "resolved": {
                "session": _jsonable(asdict(self.cfg.session)),
                "camera": _jsonable(asdict(self.cfg.camera)),
                "writer": _jsonable(asdict(self.cfg.writer)),
                "recording_profile": _jsonable(asdict(self.cfg.recording_profile)),
                "experiment": _jsonable(asdict(self.cfg.experiment)),
                "metadata": _jsonable(asdict(self.cfg.metadata)),
                "preview": _jsonable(asdict(self.cfg.preview)),
            },
            "preflight": _jsonable(asdict(self.preflight)),
            "device_info": _jsonable(self.device_info),
            "pfs_sha256_verified": sha256_file(self.cfg.camera.pfs_path),
            "experiment_metadata": self._experiment_metadata_document(),
        }
        (self.session_dir / "session.json").write_text(
            json.dumps(session_doc, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _write_experiment_metadata_json(self, summary: dict[str, Any] | None = None) -> None:
        document = self._experiment_metadata_document(summary=summary)
        self.experiment_metadata_path.write_text(
            json.dumps(document, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )

    def _experiment_metadata_document(self, summary: dict[str, Any] | None = None) -> dict[str, Any]:
        disk_usage = shutil.disk_usage(self.session_dir)
        experiment = asdict(self.cfg.experiment)
        missing_fields = self.cfg.experiment.missing_fields()
        automatic: dict[str, Any] = {
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "session_directory": str(self.session_dir),
            "session_directory_name": self.session_dir.name,
            "computer_name": socket.gethostname(),
            "camera_serial": self.cfg.camera.serial,
            "interface_card": self.device_info.get("interface_id"),
            "device_info": self.device_info,
            "camera_profile_path_resolved": str(self.cfg.camera.pfs_path),
            "pfs_sha256": self.preflight.pfs_sha256,
            "camera_settings": {
                "exposure_time": self.device_info.get("exposure_time"),
                "gain": self.device_info.get("gain"),
                "frame_rate": self.device_info.get("acquisition_frame_rate"),
                "width": self.device_info.get("width"),
                "height": self.device_info.get("height"),
                "pixel_format": self.device_info.get("pixel_format"),
            },
            "recording_profile": _jsonable(asdict(self.cfg.recording_profile)),
            "software": _software_info(),
            "disk": {
                "output_root": str(self.cfg.session.output_root),
                "session_path": str(self.session_dir),
                "preflight_free_bytes": self.preflight.output_free_bytes,
                "current_free_bytes": disk_usage.free,
                "current_free_gb": round(disk_usage.free / 1024**3, 3),
            },
            "camera_temperature_c": self.device_info.get("device_temperature_c"),
        }
        if summary:
            automatic["recording_summary"] = summary
            if summary.get("last_camera_temperature_c") is not None:
                automatic["camera_temperature_c_final"] = summary.get("last_camera_temperature_c")
            if summary.get("last_free_space_gb") is not None:
                automatic["disk"]["final_free_gb"] = summary.get("last_free_space_gb")

        return {
            "schema_version": 1,
            "metadata_complete": not missing_fields,
            "missing_fixed_fields": missing_fields,
            "fixed_fields": experiment,
            "automatic": automatic,
        }


def _software_info() -> dict[str, Any]:
    return {
        "pycamrec_version": __version__,
        "release_stage": __release_stage__,
        "python": sys.version,
        "git_commit": _git_value("rev-parse", "--short", "HEAD"),
        "git_dirty": _git_dirty(),
    }


def _git_value(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parents[1]), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    value = completed.stdout.strip()
    return value or None


def _git_dirty() -> bool | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parents[1]), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    return bool(completed.stdout.strip())


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value
