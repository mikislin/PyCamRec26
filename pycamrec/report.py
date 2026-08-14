"""Session report helpers."""

from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path
from statistics import mean, median
from typing import Any

from .hardware import session_hardware_fingerprint
from .pixel_formats import channel_semantics
from .profiles import profile_claims_losslessness


def build_session_report(session_dir: Path) -> dict[str, Any]:
    frames_path = session_dir / "frames.csv"
    segments_path = session_dir / "segments.csv"
    events_path = session_dir / "events.jsonl"
    session_path = session_dir / "session.json"
    experiment_metadata_path = session_dir / "experiment_metadata.json"
    analysis_manifest_path = session_dir / "analysis_manifest.json"
    pixel_verification_path = session_dir / "pixel_verification.json"
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
    analysis_manifest = (
        json.loads(analysis_manifest_path.read_text(encoding="utf-8"))
        if analysis_manifest_path.is_file()
        else {}
    )
    session_summary = _last_event_payload(events, "session_summary")
    finalized = bool(session_summary)
    finalization_errors = [
        event for event in events if event.get("kind") in {"finalization_error", "writer_error"}
    ]
    part_files = sorted((session_dir / "segments").glob("*.part.*"))

    block_ids = [_to_int(row.get("camera_block_id")) for row in frames]
    block_ids = [value for value in block_ids if value is not None]
    frame_indices = [_to_int(row.get("frame_index")) for row in frames]
    frame_indices = [value for value in frame_indices if value is not None]
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
    queue_frames_max = max(queue_depths) if queue_depths else 0
    queue_summary_max = _to_int_like(session_summary.get("max_queue_depth")) or 0
    queue_max = max(queue_frames_max, queue_summary_max)
    rows_at_queue_max = (
        queue_depths.count(queue_max)
        if queue_depths and queue_max == queue_frames_max
        else 0
    )
    drops = [_to_int(row.get("dropped_before_frame")) or 0 for row in frames]

    host_diffs = _diffs(host_ts)
    cam_diffs = _diffs(cam_ts)
    cam_raw_diffs = _diffs(cam_raw_ts)
    expected_fps = _nested(session, ["resolved", "camera", "expected_fps"])
    expected_frames = None
    duration_s = _nested(session, ["resolved", "session", "duration_s"])
    if expected_fps is not None and duration_s is not None:
        expected_frames = round(float(expected_fps) * float(duration_s))
    writer_config = _nested(session, ["resolved", "writer"]) or {}
    ffprobe_frames = (
        _ffprobe_total_frames(
            segments,
            session_dir,
            str(writer_config.get("ffprobe_path") or "ffprobe"),
        )
        if finalized and segments
        else None
    )
    pixel_verification = _load_json(pixel_verification_path)

    report = {
        "session_dir": str(session_dir),
        "session_state": {
            "status": "finalized" if finalized else "in_progress",
            "finalized": finalized,
            "clean_finalization": finalized and not finalization_errors and not part_files and not session_summary.get("error"),
            "finalization_error_count": len(finalization_errors),
            "partial_segment_files": [str(path) for path in part_files],
        },
        "experiment_metadata": _experiment_metadata_report(experiment_metadata, experiment_metadata_path),
        "recording_profile": _nested(session, ["resolved", "recording_profile"]) or {},
        "profile_approval": session.get("profile_approval") or {},
        "hardware_fingerprint": session.get("hardware_fingerprint")
        or (session_hardware_fingerprint(session_dir) if session_path.is_file() else {}),
        "analysis": _analysis_report(session, analysis_manifest, analysis_manifest_path),
        "pixel_verification": pixel_verification,
        "frames": {
            "count": len(frames),
            "expected": expected_frames,
            "first_frame_index": _field(frames, 0, "frame_index"),
            "last_frame_index": _field(frames, -1, "frame_index"),
            "first_block_id": block_ids[0] if block_ids else None,
            "last_block_id": block_ids[-1] if block_ids else None,
            "frames_with_block_id": len(block_ids),
            "frames_with_frame_index": len(frame_indices),
            "frames_with_camera_timestamp_raw": len(cam_raw_ts),
            "frames_with_camera_timestamp_ns": len(cam_ts),
            "block_id_gaps": _count_block_gaps(block_ids),
            "block_id_discontinuities": _count_sequence_discontinuities(block_ids),
            "frame_index_discontinuities": _count_sequence_discontinuities(frame_indices),
            "drop_sum": sum(drops),
        },
        "host_timing": _diff_report(host_diffs, scale=1_000_000_000.0),
        "camera_timestamp": _diff_report(cam_diffs, scale=1_000_000_000.0),
        "camera_timestamp_raw": _diff_report(cam_raw_diffs, scale=None),
        "queue": {
            "max_depth": queue_max,
            "rows_at_max_depth": rows_at_queue_max,
            "frames_csv_max_depth": queue_frames_max,
            "session_summary_max_depth": queue_summary_max,
        },
        "segments": {
            "count": len(segments),
            "total_bytes": sum(_to_int(row.get("size_bytes")) or 0 for row in segments),
            "total_frames": sum(_to_int(row.get("frame_count")) or 0 for row in segments),
            "ffprobe_frames": ffprobe_frames,
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


def _last_event_payload(events: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("kind") == kind and isinstance(event.get("payload"), dict):
            return event["payload"]
    return {}


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


def _count_sequence_discontinuities(values: list[int]) -> int:
    return sum(1 for previous, current in zip(values, values[1:]) if current != previous + 1)


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
        "frames_shed_queue": _to_int_like(summary.get("frames_shed_queue")) or 0,
        "frames_shed_fps": _to_int_like(summary.get("frames_shed_fps")) or 0,
        "adaptive_throttle_events": _to_int_like(summary.get("adaptive_throttle_events")) or 0,
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


def _analysis_report(session: dict[str, Any], manifest: dict[str, Any], path: Path) -> dict[str, Any]:
    camera = _nested(session, ["resolved", "camera"]) or {}
    writer = _nested(session, ["resolved", "writer"]) or {}
    pixel_format = str(camera.get("expected_pixel_format") or "")
    semantics = channel_semantics(pixel_format) if pixel_format else ""
    if manifest:
        recommendations = manifest.get("analysis_recommendations") or {}
    else:
        recommendations = {
            "preferred_mode": "gray" if semantics == "mono_luma" else "color",
            "opencv_set_cap_prop_convert_rgb_false": semantics in {"mono_luma"} or semantics.startswith("raw_bayer"),
        }
    return {
        "manifest_present": bool(manifest),
        "manifest_path": str(path),
        "source_pixel_format": pixel_format,
        "channel_semantics": semantics,
        "container": writer.get("container"),
        "input_pix_fmt": writer.get("input_pix_fmt"),
        "output_pix_fmt": writer.get("output_pix_fmt"),
        "recommendations": recommendations,
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
    session_state = report.get("session_state", {})
    pixel_verification = report.get("pixel_verification", {})
    profile_approval = report.get("profile_approval", {})
    expected_fps = _to_float(_nested(session, ["resolved", "camera", "expected_fps"]))
    queue_max_frames = _to_float(_nested(session, ["resolved", "writer", "queue_max_frames"]))

    frame_count = _to_int_like(frames.get("count"))
    expected_frames = _to_int_like(frames.get("expected"))
    segment_frames = _to_int_like(segments.get("total_frames"))
    ffprobe_frames = _to_int_like(segments.get("ffprobe_frames"))
    block_gaps = _to_int_like(frames.get("block_id_gaps")) or 0
    block_discontinuities = _to_int_like(frames.get("block_id_discontinuities")) or 0
    frame_index_discontinuities = _to_int_like(frames.get("frame_index_discontinuities")) or 0
    frames_with_block_id = _to_int_like(frames.get("frames_with_block_id")) or 0
    frames_with_frame_index = _to_int_like(frames.get("frames_with_frame_index")) or 0
    drop_sum = _to_int_like(frames.get("drop_sum")) or 0
    max_queue_depth = _to_int_like(queue.get("max_depth")) or 0
    approx_fps = _to_float(host_timing.get("approx_fps"))

    if not session_state.get("finalized"):
        return {
            "status": "in_progress",
            "acquisition_pass": False,
            "frame_continuity_pass": False,
            "realtime_timing_pass": False,
            "queue_pass": False,
            "queue_margin_pass": False,
            "pixel_pass": False,
            "metadata_pass": bool(experiment_metadata.get("metadata_complete")),
            "profile_approval_pass": bool(profile_approval.get("approved")),
            "evidence_ready": False,
            "experiment_ready": False,
            "expected_fps": expected_fps,
            "observed_host_fps": approx_fps,
            "max_queue_depth": max_queue_depth,
            "issues": [],
            "warnings": ["Recording is still in progress; final QC is intentionally deferred."],
        }

    frame_continuity_pass = (
        expected_frames is not None
        and frame_count == expected_frames
        and segment_frames == frame_count
        and ffprobe_frames == expected_frames
        and frames_with_block_id == frame_count
        and frames_with_frame_index == frame_count
        and block_gaps == 0
        and block_discontinuities == 0
        and frame_index_discontinuities == 0
        and drop_sum == 0
    )
    if not frame_continuity_pass:
        issues.append(
            "Frame continuity failed: expected, frames.csv, sequential frame/block IDs, segments.csv, "
            "FFprobe, and detected drops must agree."
        )

    clean_finalization_pass = bool(session_state.get("clean_finalization"))
    if not clean_finalization_pass:
        issues.append("Session did not finalize cleanly or contains partial segment files.")
    acquisition_pass = frame_continuity_pass and clean_finalization_pass

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

    queue_pass = False
    queue_margin_pass = False
    if queue_max_frames:
        queue_pass = max_queue_depth < int(queue_max_frames * 0.90)
        queue_margin_pass = max_queue_depth < queue_max_frames * 0.25
        if not queue_pass:
            issues.append(
                f"Queue pressure failed: max queue depth {max_queue_depth} reached at least 90% "
                f"of configured capacity {int(queue_max_frames)}."
            )
        elif not queue_margin_pass:
            warnings.append(
                f"Queue passed the 90% failure limit but exceeded the 25% profile-lock margin: "
                f"{max_queue_depth}/{int(queue_max_frames)}."
            )
    else:
        warnings.append("Queue capacity is missing; queue safety cannot pass.")

    profile_status = str(profile.get("validation_status", ""))
    if profile_status and not profile_status.startswith("validated_") and not profile_approval.get("approved"):
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

    metadata_pass = bool(experiment_metadata.get("metadata_complete"))
    lossless_claim = profile_claims_losslessness(str(profile.get("pixel_fidelity") or ""))
    pixel_status = str(pixel_verification.get("status") or "not_run")
    verified_pixel_pass = bool(
        pixel_verification.get("hashes_match")
        and pixel_status in {"pass_exact_pixels", "pass_exact_pixel_sample"}
    )
    pixel_pass = verified_pixel_pass if lossless_claim else True
    if lossless_claim and not pixel_pass:
        warnings.append(
            "This profile claims losslessness, but real-session source-vs-decoded pixel evidence is missing or failed."
        )

    approval_pass = bool(profile_approval.get("approved"))
    if not approval_pass:
        reasons = profile_approval.get("reasons") or ["no hardware-specific profile lock is recorded"]
        warnings.append("Profile approval is not valid for this run: " + "; ".join(map(str, reasons)) + ".")

    qc_pass = acquisition_pass and realtime_timing_pass and queue_pass
    evidence_ready = qc_pass and metadata_pass and (pixel_pass if lossless_claim else True)
    experiment_ready = evidence_ready and approval_pass

    if qc_pass:
        status = "pass"
    elif acquisition_pass:
        status = "fail_realtime"
    elif not clean_finalization_pass:
        status = "fail_finalization"
    else:
        status = "fail_frame_integrity"

    return {
        "status": status,
        "acquisition_pass": acquisition_pass,
        "frame_continuity_pass": frame_continuity_pass,
        "clean_finalization_pass": clean_finalization_pass,
        "realtime_timing_pass": realtime_timing_pass,
        "queue_pass": queue_pass,
        "queue_margin_pass": queue_margin_pass,
        "qc_pass": qc_pass,
        "pixel_verification_required": lossless_claim,
        "pixel_status": pixel_status,
        "pixel_pass": pixel_pass,
        "metadata_pass": metadata_pass,
        "profile_approval_pass": approval_pass,
        "evidence_ready": evidence_ready,
        "experiment_ready": experiment_ready,
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


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _ffprobe_total_frames(
    segments: list[dict[str, str]],
    session_dir: Path,
    ffprobe_path: str,
) -> int | None:
    total = 0
    for row in segments:
        path = Path(str(row.get("path") or ""))
        if not path.is_absolute():
            path = session_dir / path
        try:
            completed = subprocess.run(
                [
                    ffprobe_path,
                    "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=nb_frames",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            value = _to_int_like(completed.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            return None
        if value is None:
            return None
        total += value
    return total if segments else None
