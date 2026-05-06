"""Session report helpers."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from statistics import mean, median
from typing import Any


def build_session_report(session_dir: Path) -> dict[str, Any]:
    frames_path = session_dir / "frames.csv"
    segments_path = session_dir / "segments.csv"
    events_path = session_dir / "events.jsonl"
    session_path = session_dir / "session.json"
    experiment_metadata_path = session_dir / "experiment_metadata.json"
    if not frames_path.is_file():
        raise FileNotFoundError(f"frames.csv not found: {frames_path}")

    frames = _read_csv(frames_path)
    segments = _read_csv(segments_path) if segments_path.is_file() else []
    events = _read_events(events_path) if events_path.is_file() else []
    session = json.loads(session_path.read_text(encoding="utf-8")) if session_path.is_file() else {}
    experiment_metadata = (
        json.loads(experiment_metadata_path.read_text(encoding="utf-8"))
        if experiment_metadata_path.is_file()
        else {}
    )

    block_ids = [_to_int(row.get("camera_block_id")) for row in frames]
    block_ids = [value for value in block_ids if value is not None]
    host_ts = [_to_int(row.get("host_receive_perf_counter_ns")) for row in frames]
    host_ts = [value for value in host_ts if value is not None]
    cam_ts = [_to_int(row.get("camera_timestamp_ns")) for row in frames]
    cam_ts = [value for value in cam_ts if value is not None]
    cam_raw_ts = [_to_int(row.get("camera_timestamp_raw")) for row in frames]
    cam_raw_ts = [value for value in cam_raw_ts if value is not None]
    if not cam_raw_ts and cam_ts and median(_diffs(cam_ts) or [0]) < 1_000_000:
        cam_raw_ts = cam_ts
        cam_ts = []
    queue_depths = [_to_int(row.get("queue_depth_after_enqueue")) or 0 for row in frames]
    drops = [_to_int(row.get("dropped_before_frame")) or 0 for row in frames]

    host_diffs = _diffs(host_ts)
    cam_diffs = _diffs(cam_ts)
    cam_raw_diffs = _diffs(cam_raw_ts)
    expected_fps = _nested(session, ["resolved", "camera", "expected_fps"])
    expected_frames = None
    duration_s = _nested(session, ["resolved", "session", "duration_s"])
    if expected_fps is not None and duration_s is not None:
        expected_frames = round(float(expected_fps) * float(duration_s))

    report = {
        "session_dir": str(session_dir),
        "experiment_metadata": _experiment_metadata_report(experiment_metadata, experiment_metadata_path),
        "recording_profile": _nested(session, ["resolved", "recording_profile"]) or {},
        "frames": {
            "count": len(frames),
            "expected": expected_frames,
            "first_frame_index": _field(frames, 0, "frame_index"),
            "last_frame_index": _field(frames, -1, "frame_index"),
            "first_block_id": block_ids[0] if block_ids else None,
            "last_block_id": block_ids[-1] if block_ids else None,
            "frames_with_block_id": len(block_ids),
            "frames_with_camera_timestamp_raw": len(cam_raw_ts),
            "frames_with_camera_timestamp_ns": len(cam_ts),
            "block_id_gaps": _count_block_gaps(block_ids),
            "drop_sum": sum(drops),
        },
        "host_timing": _diff_report(host_diffs, scale=1_000_000_000.0),
        "camera_timestamp": _diff_report(cam_diffs, scale=1_000_000_000.0),
        "camera_timestamp_raw": _diff_report(cam_raw_diffs, scale=None),
        "queue": {
            "max_depth": max(queue_depths) if queue_depths else 0,
            "rows_at_max_depth": queue_depths.count(max(queue_depths)) if queue_depths else 0,
        },
        "segments": {
            "count": len(segments),
            "total_bytes": sum(_to_int(row.get("size_bytes")) or 0 for row in segments),
            "total_frames": sum(_to_int(row.get("frame_count")) or 0 for row in segments),
            "approx_mbps": _approx_mbps(segments, host_diffs),
        },
        "writer_spool": _writer_spool_report(events),
        "preview": _preview_report(events),
        "health": _health_report(events),
    }
    report["qc"] = _qc_report(report, session)
    return report


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _read_events(path: Path) -> list[dict[str, Any]]:
    events = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _to_int(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _diffs(values: list[int]) -> list[int]:
    return [values[index] - values[index - 1] for index in range(1, len(values))]


def _diff_report(diffs: list[int], scale: float | None) -> dict[str, Any]:
    if not diffs:
        return {}
    report: dict[str, Any] = {
        "count": len(diffs),
        "min": min(diffs),
        "median": median(diffs),
        "mean": mean(diffs),
        "max": max(diffs),
    }
    if scale is not None and mean(diffs) > 0:
        report["approx_fps"] = scale / mean(diffs)
    return report


def _count_block_gaps(block_ids: list[int]) -> int:
    gaps = 0
    for index in range(1, len(block_ids)):
        delta = block_ids[index] - block_ids[index - 1]
        if delta > 1:
            gaps += delta - 1
    return gaps


def _field(rows: list[dict[str, str]], index: int, field: str) -> str | None:
    if not rows:
        return None
    return rows[index].get(field)


def _nested(data: dict[str, Any], path: list[str]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _approx_mbps(segments: list[dict[str, str]], host_diffs: list[int]) -> float | None:
    total_bytes = sum(_to_int(row.get("size_bytes")) or 0 for row in segments)
    if not total_bytes or not host_diffs:
        return None
    duration_s = sum(host_diffs) / 1_000_000_000.0
    if duration_s <= 0:
        return None
    return (total_bytes * 8) / duration_s / 1_000_000


def _health_report(events: list[dict[str, Any]]) -> dict[str, Any]:
    health_events = [
        event
        for event in events
        if event.get("kind") == "segment_health" and isinstance(event.get("payload"), dict)
    ]
    if not health_events:
        return {}

    free_values = [
        value
        for value in (
            _to_float(event["payload"].get("free_space_gb")) for event in health_events
        )
        if value is not None
    ]
    temperature_values = [
        value
        for value in (
            _to_float(event["payload"].get("camera_temperature_c")) for event in health_events
        )
        if value is not None
    ]
    warning_events = [
        event
        for event in health_events
        if event["payload"].get("warnings") or event["payload"].get("status") == "warning"
    ]
    last_payload = health_events[-1]["payload"]
    return {
        "checks": len(health_events),
        "warning_checks": len(warning_events),
        "min_free_space_gb": min(free_values) if free_values else None,
        "last_free_space_gb": _to_float(last_payload.get("free_space_gb")),
        "max_camera_temperature_c": max(temperature_values) if temperature_values else None,
        "last_camera_temperature_c": _to_float(last_payload.get("camera_temperature_c")),
        "last_status": last_payload.get("status"),
    }


def _writer_spool_report(events: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [
        event.get("payload")
        for event in events
        if event.get("kind") == "session_summary" and isinstance(event.get("payload"), dict)
    ]
    if not summaries:
        return {}
    summary = summaries[-1]
    enabled = bool(summary.get("writer_spool_enabled"))
    total_bytes = _to_int_like(summary.get("writer_spool_total_bytes")) or 0
    max_bytes = _to_int_like(summary.get("writer_spool_max_queued_bytes")) or 0
    limit_bytes = _to_int_like(summary.get("writer_spool_limit_bytes")) or 0
    report: dict[str, Any] = {
        "enabled": enabled,
        "total_bytes": total_bytes,
        "max_queued_bytes": max_bytes,
        "limit_bytes": limit_bytes,
    }
    if limit_bytes:
        report["max_queued_percent"] = max_bytes / limit_bytes * 100
    return report


def _preview_report(events: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [
        event.get("payload")
        for event in events
        if event.get("kind") == "preview_summary" and isinstance(event.get("payload"), dict)
    ]
    if not summaries:
        return {}
    summary = summaries[-1]
    return {
        "enabled": bool(summary.get("enabled")),
        "sink": summary.get("sink") or "",
        "running": bool(summary.get("running")),
        "sample_every": _to_int_like(summary.get("sample_every")),
        "frames_published": _to_int_like(summary.get("frames_published")) or 0,
        "frames_displayed": _to_int_like(summary.get("frames_displayed")) or 0,
        "frames_dropped": _to_int_like(summary.get("frames_dropped")) or 0,
        "target_width": _to_int_like(summary.get("target_width")),
        "target_height": _to_int_like(summary.get("target_height")),
        "target_fps": _to_float(summary.get("target_fps")),
        "error": summary.get("error") or "",
    }


def _experiment_metadata_report(document: dict[str, Any], path: Path) -> dict[str, Any]:
    if not document:
        return {
            "metadata_complete": False,
            "missing_fixed_fields": [],
            "fixed_fields": {},
            "file_present": False,
            "path": str(path),
        }
    return {
        "metadata_complete": document.get("metadata_complete"),
        "missing_fixed_fields": document.get("missing_fixed_fields") or [],
        "fixed_fields": document.get("fixed_fields") or {},
        "file_present": True,
        "path": str(path),
    }


def _qc_report(report: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []

    frames = report.get("frames", {})
    host_timing = report.get("host_timing", {})
    queue = report.get("queue", {})
    segments = report.get("segments", {})
    preview = report.get("preview", {})
    health = report.get("health", {})
    experiment_metadata = report.get("experiment_metadata", {})
    profile = report.get("recording_profile", {})
    expected_fps = _to_float(_nested(session, ["resolved", "camera", "expected_fps"]))
    queue_max_frames = _to_float(_nested(session, ["resolved", "writer", "queue_max_frames"]))

    frame_count = _to_int_like(frames.get("count"))
    expected_frames = _to_int_like(frames.get("expected"))
    segment_frames = _to_int_like(segments.get("total_frames"))
    block_gaps = _to_int_like(frames.get("block_id_gaps")) or 0
    drop_sum = _to_int_like(frames.get("drop_sum")) or 0
    max_queue_depth = _to_int_like(queue.get("max_depth")) or 0
    approx_fps = _to_float(host_timing.get("approx_fps"))

    frame_continuity_pass = (
        expected_frames is not None
        and frame_count == expected_frames
        and segment_frames == frame_count
        and block_gaps == 0
        and drop_sum == 0
    )
    if not frame_continuity_pass:
        issues.append("Frame continuity failed: count, segment total, block gaps, or drop sum did not pass.")

    realtime_timing_pass = False
    if expected_fps is not None and approx_fps is not None:
        realtime_timing_pass = approx_fps >= expected_fps * 0.98
        if not realtime_timing_pass:
            issues.append(
                f"Real-time timing failed: host receive rate {approx_fps:.2f} fps is below "
                f"98% of expected {expected_fps:.2f} fps."
            )
    else:
        warnings.append("Real-time timing could not be evaluated from host timestamps.")

    queue_pass = True
    if queue_max_frames:
        queue_pass = max_queue_depth < int(queue_max_frames * 0.90)
        if not queue_pass:
            issues.append(
                f"Queue pressure failed: max queue depth {max_queue_depth} reached at least 90% "
                f"of configured capacity {int(queue_max_frames)}."
            )

    profile_status = str(profile.get("validation_status", ""))
    if profile_status and not profile_status.startswith("validated_"):
        warnings.append(f"Profile is not validated for scientific real-time use: {profile_status}.")

    if health.get("warning_checks"):
        warnings.append("Health warnings were recorded; inspect temperature and free-space trend.")

    if preview.get("enabled") and (not realtime_timing_pass or not queue_pass):
        warnings.append("Preview was enabled during a run with real-time or queue pressure; rerun with preview off or at lower FPS/width for scientific validation.")

    if not experiment_metadata.get("file_present", True):
        warnings.append("experiment_metadata.json was not found; session predates Phase 3 metadata or metadata write failed.")
    elif experiment_metadata.get("metadata_complete") is False:
        missing = experiment_metadata.get("missing_fixed_fields") or []
        if missing:
            warnings.append("Experiment metadata is incomplete: " + ", ".join(str(item) for item in missing) + ".")

    if frame_continuity_pass and realtime_timing_pass and queue_pass:
        status = "pass"
    elif frame_continuity_pass:
        status = "fail_realtime"
    else:
        status = "fail_frame_integrity"

    return {
        "status": status,
        "frame_continuity_pass": frame_continuity_pass,
        "realtime_timing_pass": realtime_timing_pass,
        "queue_pass": queue_pass,
        "expected_fps": expected_fps,
        "observed_host_fps": approx_fps,
        "max_queue_depth": max_queue_depth,
        "issues": issues,
        "warnings": warnings,
    }


def _to_int_like(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
