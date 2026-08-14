"""Read-only qualification planning for expensive hardware validation runs."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import load_config
from .profiles import profile_claims_losslessness


def build_qualification_plan(config_path: Path) -> dict[str, Any]:
    config_path = config_path.expanduser().resolve()
    cfg = load_config(config_path, duration_s=1.0)
    policy = cfg.raw.get("qualification") if isinstance(cfg.raw, dict) else {}
    policy = policy if isinstance(policy, dict) else {}
    durations = _float_list(policy.get("durations_s") or [30, 60])
    repeats = int(policy.get("required_repeats", 3))
    preview_modes = [str(item).lower() for item in policy.get("preview_modes", ["off"])]
    preview_sink = str(policy.get("preview_sink") or cfg.preview.sink)
    if repeats < 3:
        raise ValueError("qualification.required_repeats must be at least 3.")
    if not durations or any(duration <= 0 for duration in durations):
        raise ValueError("qualification.durations_s must contain positive durations.")
    if any(mode not in {"off", "on"} for mode in preview_modes):
        raise ValueError("qualification.preview_modes may contain only 'off' and 'on'.")
    bitrate = cfg.writer.expected_bitrate_mbps or cfg.recording_profile.expected_bitrate_mbps
    if bitrate is None or bitrate <= 0:
        raise ValueError("A positive expected bitrate is required to budget qualification storage.")
    cases = []
    total_bytes = 0
    for mode in preview_modes:
        for duration in durations:
            estimated_bytes = int(round(bitrate * 1_000_000 / 8 * duration))
            total_bytes += estimated_bytes * repeats
            cases.append(
                {
                    "preview_mode": mode,
                    "duration_s": duration,
                    "repetitions": repeats,
                    "estimated_bytes_per_run": estimated_bytes,
                    "estimated_decimal_gb_per_run": round(estimated_bytes / 1_000_000_000, 3),
                    "estimated_decimal_gb_all_repetitions": round(
                        estimated_bytes * repeats / 1_000_000_000, 3
                    ),
                }
            )
    duration_text = ",".join(f"{duration:g}" for duration in durations)
    preview_arg = "both" if set(preview_modes) == {"off", "on"} else preview_modes[0]
    pixel_args = (
        " --verify-session-pixels --pixel-max-decode-frames 1000 "
        "--source-frame-hash-every 10 --source-frame-hash-max-frames 100"
        if profile_claims_losslessness(cfg.recording_profile.pixel_fidelity)
        else ""
    )
    task_quality_required = bool(policy.get("require_task_quality_record", False))
    task_quality_arg = (
        " --task-quality-record PATH_TO_TASK_QUALITY_RECORD.json"
        if task_quality_required
        else ""
    )
    command_tail = (
        f" validation-sweep \"{config_path}\" --durations {duration_text} "
        f"--preview {preview_arg} --repeats {repeats} --required-passing-repeats {repeats}"
        f"{pixel_args}{task_quality_arg} --require-complete-metadata"
    )
    return {
        "schema_version": 1,
        "qualification_policy": "cxp_profile_lock_v2",
        "config": str(config_path),
        "profile_id": cfg.recording_profile.id,
        "pixel_fidelity": cfg.recording_profile.pixel_fidelity,
        "expected_bitrate_mbps": bitrate,
        "validated_max_duration_s_if_all_pass": max(durations),
        "required_repetitions_at_max_duration": repeats,
        "preview_modes": preview_modes,
        "preview_sink": preview_sink,
        "task_quality_record_required": task_quality_required,
        "cases": cases,
        "estimated_total_bytes": total_bytes,
        "estimated_total_decimal_gb": round(total_bytes / 1_000_000_000, 3),
        "recommended_free_space_gb_before_start": round(
            total_bytes / 1_000_000_000 * 1.2 + cfg.writer.min_free_space_gb,
            1,
        ),
        "powershell_command": f"& $PY -m pycamrec{command_tail}",
        "cmd_command": f'"%PY%" -m pycamrec{command_tail}',
        "notes": [
            "This command records real camera data; review the storage budget before running it.",
            "Every intended preview mode is qualified and locked independently.",
            "Lossy profiles additionally require an external task-quality acceptance record.",
        ],
    }


def load_task_quality_record(
    path: Path | None,
    *,
    profile_id: str,
    profile_version: str,
    required: bool,
) -> dict[str, Any]:
    if path is None:
        return {
            "required": required,
            "present": False,
            "pass": not required,
            "issues": ["task quality record is required"] if required else [],
        }
    path = path.expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"required": required, "present": False, "pass": False, "issues": [str(exc)]}
    issues = []
    if not isinstance(document, dict):
        issues.append("task quality record must be a JSON object")
        document = {}
    if document.get("schema_version") != 1:
        issues.append("schema_version must be 1")
    if document.get("profile_id") != profile_id:
        issues.append("profile_id does not match the qualification profile")
    if document.get("profile_version") != profile_version:
        issues.append("profile_version does not match the qualification profile")
    if document.get("status") != "pass":
        issues.append("status must be 'pass'")
    try:
        evaluated = datetime.fromisoformat(str(document.get("evaluated_utc") or "").replace("Z", "+00:00"))
        if evaluated.tzinfo is None:
            raise ValueError
    except ValueError:
        issues.append("evaluated_utc must be a timezone-aware ISO-8601 timestamp")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", str(document.get("reference_dataset_sha256") or "")):
        issues.append("reference_dataset_sha256 must contain 64 hexadecimal characters")
    if not isinstance(document.get("acceptance_criteria"), dict) or not document.get("acceptance_criteria"):
        issues.append("acceptance_criteria must be a non-empty object")
    if not isinstance(document.get("metrics"), dict) or not document.get("metrics"):
        issues.append("metrics must be a non-empty object")
    return {
        "required": required,
        "present": True,
        "pass": not issues,
        "path": str(path),
        "sha256": _sha256_file(path),
        "issues": issues,
        "document": document,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _float_list(value: Any) -> list[float]:
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        items = [value]
    return [float(item) for item in items if item not in (None, "")]
