"""Read-only qualification planning for expensive hardware validation runs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import load_config
from .profiles import profile_claims_losslessness


DEFAULT_TASK_QUALITY_CRITERIA = {
    "tracking_median_error_px_max": 1.0,
    "segmentation_iou_min": 0.95,
    "event_f1_min": 0.95,
}
DEFAULT_TASK_QUALITY_METRICS = {
    "tracking_median_error_px": None,
    "segmentation_iou": None,
    "event_f1": None,
}


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
    command_tail = (
        f" validation-sweep \"{config_path}\" --durations {duration_text} "
        f"--preview {preview_arg} --repeats {repeats} --required-passing-repeats {repeats}"
        f"{pixel_args} --require-complete-metadata"
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
            (
                "Lossy sweeps create a pending task_quality_record.json automatically; "
                "real downstream metrics are supplied during finalization."
                if task_quality_required
                else "This profile does not require downstream task-quality evidence."
            ),
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
        issues.append("computed status must be 'pass'")
    try:
        evaluated = datetime.fromisoformat(str(document.get("evaluated_utc") or "").replace("Z", "+00:00"))
        if evaluated.tzinfo is None:
            raise ValueError
    except ValueError:
        issues.append("evaluated_utc must be a timezone-aware ISO-8601 timestamp")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", str(document.get("reference_dataset_sha256") or "")):
        issues.append("reference_dataset_sha256 must contain 64 hexadecimal characters")
    criteria = document.get("acceptance_criteria")
    metrics = document.get("metrics")
    if not isinstance(criteria, dict) or not criteria:
        issues.append("acceptance_criteria must be a non-empty object")
    if not isinstance(metrics, dict) or not metrics:
        issues.append("metrics must be a non-empty object")
    metric_issues, calculated_status = evaluate_task_quality_metrics(criteria, metrics)
    issues.extend(metric_issues)
    if calculated_status != "pass":
        issues.append("task metrics do not pass every acceptance criterion")
    return {
        "required": required,
        "present": True,
        "pass": not issues,
        "path": str(path),
        "sha256": _sha256_file(path),
        "issues": issues,
        "document": document,
    }


def evaluate_task_quality_metrics(
    acceptance_criteria: Any,
    metrics: Any,
) -> tuple[list[str], str]:
    """Validate paired ``*_min``/``*_max`` criteria and compute their status."""
    if not isinstance(acceptance_criteria, dict) or not acceptance_criteria:
        return ["acceptance_criteria must be a non-empty object"], "pending"
    if not isinstance(metrics, dict) or not metrics:
        return ["metrics must be a non-empty object"], "pending"
    issues: list[str] = []
    incomplete = False
    failed = False
    for criterion_name, threshold_value in acceptance_criteria.items():
        suffix = "_min" if str(criterion_name).endswith("_min") else "_max" if str(criterion_name).endswith("_max") else ""
        if not suffix:
            issues.append(f"acceptance criterion {criterion_name!r} must end with '_min' or '_max'")
            failed = True
            continue
        metric_name = str(criterion_name)[: -len(suffix)]
        threshold = _finite_float(threshold_value)
        metric = _finite_float(metrics.get(metric_name))
        if threshold is None:
            issues.append(f"acceptance criterion {criterion_name!r} must be a finite number")
            failed = True
            continue
        if metric is None:
            issues.append(f"metric {metric_name!r} must be a finite number")
            incomplete = True
            continue
        if suffix == "_min" and metric < threshold:
            issues.append(f"metric {metric_name!r}={metric:g} is below minimum {threshold:g}")
            failed = True
        elif suffix == "_max" and metric > threshold:
            issues.append(f"metric {metric_name!r}={metric:g} exceeds maximum {threshold:g}")
            failed = True
    if failed:
        return issues, "fail"
    if incomplete:
        return issues, "pending"
    return issues, "pass"


def update_task_quality_record_from_sweep(
    output_path: Path,
    *,
    source_path: Path | None,
    profile_id: str,
    profile_version: str,
    validation_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Create/update the sweep-bound working task-quality record.

    Acquisition evidence is recorded here, but it never substitutes for the
    downstream tracking/segmentation/event metrics.
    """
    document: dict[str, Any] = {}
    identity_matches = False
    if source_path is not None:
        source_path = source_path.expanduser().resolve()
        try:
            loaded = json.loads(source_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                document = loaded
                identity_matches = (
                    document.get("profile_id") == profile_id
                    and document.get("profile_version") == profile_version
                )
        except (OSError, json.JSONDecodeError):
            document = {}
    if document and not identity_matches:
        document = {
            "acceptance_criteria": dict(DEFAULT_TASK_QUALITY_CRITERIA),
            "metrics": dict(DEFAULT_TASK_QUALITY_METRICS),
            "reference_dataset_sha256": "",
            "reference_dataset_name": "",
            "evaluated_utc": None,
            "notes": (
                "Prior task metrics were cleared because the profile ID or version changed; "
                "evaluate this profile against the immutable reference dataset."
            ),
        }
    document["schema_version"] = 1
    document["profile_id"] = profile_id
    document["profile_version"] = profile_version
    criteria = document.get("acceptance_criteria")
    if not isinstance(criteria, dict) or not criteria:
        criteria = dict(DEFAULT_TASK_QUALITY_CRITERIA)
    metrics = document.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    for metric_name, value in DEFAULT_TASK_QUALITY_METRICS.items():
        metrics.setdefault(metric_name, value)
    document["acceptance_criteria"] = criteria
    document["metrics"] = metrics
    document["validation_evidence"] = validation_evidence
    metric_issues, metric_status = evaluate_task_quality_metrics(criteria, metrics)
    dataset_hash = str(document.get("reference_dataset_sha256") or "")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", dataset_hash):
        document["reference_dataset_sha256"] = ""
        document["reference_dataset_name"] = ""
        metric_status = "pending"
    document["status"] = metric_status
    if metric_status in {"pass", "fail"}:
        try:
            evaluated = datetime.fromisoformat(
                str(document.get("evaluated_utc") or "").replace("Z", "+00:00")
            )
            if evaluated.tzinfo is None:
                raise ValueError
        except ValueError:
            document["evaluated_utc"] = datetime.now(timezone.utc).isoformat()
    else:
        document["evaluated_utc"] = None
    notes = str(document.get("notes") or "")
    if not notes or "set status" in notes.lower() or "replace placeholder" in notes.lower():
        document["notes"] = (
            "Status is calculated from the immutable reference dataset hash and all configured metrics; "
            "acquisition evidence alone cannot pass this gate."
        )
    _atomic_write_json(output_path, document)
    if source_path is not None and source_path != output_path.expanduser().resolve():
        _atomic_write_json(source_path, document)
    return {
        "path": str(output_path.expanduser().resolve()),
        "status": metric_status,
        "metric_issues": metric_issues,
        "document": document,
    }


def set_task_quality_measurements(
    path: Path,
    *,
    reference_dataset_path: Path,
    metrics: dict[str, float],
) -> dict[str, Any]:
    """Hash an immutable evaluation artifact and store measured task metrics."""
    path = path.expanduser().resolve()
    reference_dataset_path = reference_dataset_path.expanduser().resolve()
    if not reference_dataset_path.is_file():
        raise FileNotFoundError(f"Reference dataset/manifest is not a file: {reference_dataset_path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Task-quality record must contain a JSON object.")
    current_metrics = document.get("metrics")
    current_metrics = dict(current_metrics) if isinstance(current_metrics, dict) else {}
    for name, value in metrics.items():
        numeric = _finite_float(value)
        if numeric is None:
            raise ValueError(f"Task metric {name!r} must be a finite number.")
        current_metrics[name] = numeric
    document["metrics"] = current_metrics
    document["reference_dataset_sha256"] = _sha256_file(reference_dataset_path)
    document["reference_dataset_name"] = reference_dataset_path.name
    issues, status = evaluate_task_quality_metrics(document.get("acceptance_criteria"), current_metrics)
    document["status"] = status
    document["evaluated_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(path, document)
    return {"path": str(path), "status": status, "issues": issues, "document": document}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, document: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _float_list(value: Any) -> list[float]:
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        items = [value]
    return [float(item) for item in items if item not in (None, "")]
