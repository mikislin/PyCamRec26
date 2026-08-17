"""Create hardware-specific approved configs from validation evidence."""

from __future__ import annotations

import json
import hashlib
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import load_config
from .hardware import build_profile_fingerprint
from .qualification import (
    load_task_quality_record,
    set_task_quality_measurements,
    update_task_quality_record_from_sweep,
)


def lock_profile_from_summary(
    config_path: Path,
    summary_path: Path,
    output_path: Path,
    *,
    preview_mode: str,
) -> dict[str, Any]:
    preview_mode = preview_mode.strip().lower()
    if preview_mode not in {"on", "off"}:
        raise ValueError("preview_mode must be 'on' or 'off'.")
    config_path = config_path.expanduser().resolve()
    summary_path = summary_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if output_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing approved config: {output_path}")

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    mode_key = f"preview_{preview_mode}"
    recommendation = str((summary.get("profile_lock_by_preview_mode") or {}).get(mode_key) or "")
    if not recommendation.startswith("lock_validated_for_this_evidence_fingerprint_"):
        raise ValueError(
            f"Validation summary does not permit locking {mode_key}: {recommendation or 'missing recommendation'}"
        )
    expected_preview = preview_mode == "on"
    candidates = [
        result
        for result in summary.get("results") or []
        if bool(result.get("preview_enabled")) == expected_preview
        and bool(result.get("evidence_ready"))
        and bool(result.get("preferred_queue_pass"))
    ]
    if not candidates:
        raise ValueError(f"No evidence-ready, queue-margin passing {mode_key} result was found.")
    validated_max_duration_s = max(float(result.get("duration_s") or 0) for result in candidates)
    max_duration_candidates = [
        result
        for result in candidates
        if float(result.get("duration_s") or 0) == validated_max_duration_s
    ]
    fingerprints = {
        str(result.get("hardware_fingerprint_sha256") or "")
        for result in max_duration_candidates
        if result.get("hardware_fingerprint_sha256")
    }
    if len(fingerprints) != 1:
        raise ValueError("Lock candidates have mixed or missing evidence fingerprints.")
    fingerprint = next(iter(fingerprints))
    profile_fingerprints = {
        str(result.get("profile_fingerprint_sha256") or "")
        for result in max_duration_candidates
        if result.get("profile_fingerprint_sha256")
    }
    if len(profile_fingerprints) != 1:
        raise ValueError("Lock candidates have mixed or missing resolved profile fingerprints.")
    profile_fingerprint = next(iter(profile_fingerprints))
    required_repeats = int(
        ((summary.get("requirements") or {}).get("passing_repeats_at_max_duration_required"))
        or 3
    )
    if len(max_duration_candidates) < required_repeats:
        raise ValueError(
            f"Lock requires {required_repeats} passing repetitions at {validated_max_duration_s:g}s; "
            f"found {len(max_duration_candidates)}."
        )
    config = load_config(config_path, duration_s=1.0)
    profile_ids = {str(result.get("profile_id") or "") for result in candidates}
    if profile_ids != {config.recording_profile.id}:
        raise ValueError(
            f"Evidence profile IDs {sorted(profile_ids)!r} do not match config profile {config.recording_profile.id!r}."
        )

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Config must contain a mapping: {config_path}")
    data["approval"] = {
        "status": "locked_for_evidence_fingerprint",
        "intended_preview_mode": preview_mode,
        "evidence_fingerprint_sha256": fingerprint,
        "profile_fingerprint_sha256": profile_fingerprint,
        "validated_max_duration_s": validated_max_duration_s,
        "passing_repetitions_at_max_duration": len(max_duration_candidates),
        "qualification_policy_schema_version": 2,
        "task_quality_record_required": bool(
            ((summary.get("requirements") or {}).get("task_quality_record_required"))
        ),
        "task_quality_record_sha256": str(
            ((summary.get("task_quality_record") or {}).get("sha256")) or ""
        ),
        "validation_summary_path": str(summary_path),
        "validation_summary_sha256": _sha256_file(summary_path),
        "locked_utc": datetime.now(timezone.utc).isoformat(),
        "recommendation": recommendation,
    }
    preview = dict(data.get("preview", {}))
    evidence_session_path = Path(str(max_duration_candidates[0].get("session_dir") or "")) / "session.json"
    if not evidence_session_path.is_file():
        raise ValueError(f"Lock candidate session evidence is unavailable: {evidence_session_path}")
    evidence_session = json.loads(evidence_session_path.read_text(encoding="utf-8"))
    resolved = evidence_session.get("resolved")
    resolved = resolved if isinstance(resolved, dict) else {}
    validated_preview = resolved.get("preview")
    validated_preview = validated_preview if isinstance(validated_preview, dict) else {}
    stable_preview_keys = {
        "enabled",
        "max_fps",
        "width",
        "height",
        "overlay",
        "sample_every",
        "sink",
        "shed_queue_fraction",
        "shed_fps_ratio",
        "throttle_cooldown_s",
        "opencv_threads",
    }
    for key in stable_preview_keys:
        if key in validated_preview:
            preview[key] = validated_preview[key]
    preview["enabled"] = expected_preview
    if preview.get("sink") in {"shm", "shm_raw"}:
        preview["image_path"] = str(output_path.with_suffix(".preview.pgm"))
    data["preview"] = preview
    if isinstance(data.get("profile"), str):
        data["profile"] = {"id": data["profile"]}
    camera = dict(data.get("camera", {}))
    camera["pfs_path"] = str(config.camera.pfs_path)
    data["camera"] = camera
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(data, sort_keys=False)
    output_path.write_text(rendered, encoding="utf-8")
    locked_config = load_config(output_path, duration_s=min(1.0, validated_max_duration_s))
    locked_fingerprint = build_profile_fingerprint(
        camera_config=asdict(locked_config.camera),
        writer_config=asdict(locked_config.writer),
        recording_profile=asdict(locked_config.recording_profile),
        preview_config=asdict(locked_config.preview),
    )
    if locked_fingerprint["fingerprint_sha256"] != profile_fingerprint:
        output_path.unlink(missing_ok=True)
        raise ValueError(
            "Approved config does not reproduce the validated profile fingerprint. "
            "Use the same writer, segment, bitrate, and preview settings as the qualification cases."
        )
    return {
        "output_path": str(output_path),
        "profile_id": config.recording_profile.id,
        "preview_mode": preview_mode,
        "evidence_fingerprint_sha256": fingerprint,
        "profile_fingerprint_sha256": profile_fingerprint,
        "validated_max_duration_s": validated_max_duration_s,
        "validation_summary_path": str(summary_path),
    }


def finalize_qualification_from_summary(
    config_path: Path,
    summary_path: Path,
    task_quality_record_path: Path | None,
    approved_dir: Path,
    *,
    reference_dataset_path: Path | None = None,
    task_metrics: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Reassess task quality, update profile status, and create both mode locks.

    This does not rerun acquisition and never converts missing downstream task
    metrics into a pass.
    """
    from .validation_sweep import SweepResult, _profile_lock_recommendation

    config_path = config_path.expanduser().resolve()
    summary_path = summary_path.expanduser().resolve()
    approved_dir = approved_dir.expanduser().resolve()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict):
        raise ValueError("Validation summary must contain a JSON object.")
    config = load_config(config_path, duration_s=1.0)
    requirements = summary.get("requirements")
    requirements = requirements if isinstance(requirements, dict) else {}
    task_required = bool(requirements.get("task_quality_record_required"))
    raw_results = summary.get("results") or []
    if not isinstance(raw_results, list) or not raw_results:
        raise ValueError("Validation summary has no sweep results.")
    result_fields = set(SweepResult.__dataclass_fields__)
    results = [
        SweepResult(**{name: raw[name] for name in result_fields})
        for raw in raw_results
        if isinstance(raw, dict) and result_fields.issubset(raw)
    ]
    if len(results) != len(raw_results):
        raise ValueError("Validation summary contains incomplete or incompatible sweep results.")
    profile_ids = {result.profile_id for result in results}
    if profile_ids != {config.recording_profile.id}:
        raise ValueError(
            f"Sweep profile IDs {sorted(profile_ids)!r} do not match {config.recording_profile.id!r}."
        )
    if task_required:
        task_quality_record_path = (
            task_quality_record_path.expanduser().resolve()
            if task_quality_record_path is not None
            else summary_path.with_name("task_quality_record.json")
        )
        update_task_quality_record_from_sweep(
            task_quality_record_path,
            source_path=task_quality_record_path if task_quality_record_path.is_file() else None,
            profile_id=config.recording_profile.id,
            profile_version=config.recording_profile.version,
            validation_evidence={
                "summary_file": summary_path.name,
                "sweep_completed_utc": datetime.now(timezone.utc).isoformat(),
                "hardware_fingerprint_sha256_values": sorted(
                    {
                        result.hardware_fingerprint_sha256
                        for result in results
                        if result.hardware_fingerprint_sha256
                    }
                ),
                "case_count": len(results),
                "acquisition_pass_count": sum(result.acquisition_pass for result in results),
                "qc_pass_count": sum(result.qc_pass for result in results),
                "evidence_ready_count": sum(result.evidence_ready for result in results),
                "all_acquisition_cases_pass": all(result.acquisition_pass for result in results),
                "all_qc_cases_pass": all(result.qc_pass for result in results),
                "all_evidence_cases_ready": all(result.evidence_ready for result in results),
                "preview_modes_exercised": sorted(
                    {"on" if result.preview_enabled else "off" for result in results}
                ),
            },
        )
        if reference_dataset_path is not None or task_metrics is not None:
            if reference_dataset_path is None or task_metrics is None:
                raise ValueError("Reference dataset and all task metrics must be supplied together.")
            set_task_quality_measurements(
                task_quality_record_path,
                reference_dataset_path=reference_dataset_path,
                metrics=task_metrics,
            )
    task_quality = load_task_quality_record(
        task_quality_record_path,
        profile_id=config.recording_profile.id,
        profile_version=config.recording_profile.version,
        required=task_required,
    )
    verify_pixels = bool(requirements.get("pixel_verification_required"))
    required_repeats = int(requirements.get("passing_repeats_at_max_duration_required") or 3)
    summary["task_quality_record"] = task_quality
    summary["profile_lock_recommendation"] = _profile_lock_recommendation(
        results,
        verify_pixels,
        True,
        required_repeats,
        task_required,
        bool(task_quality.get("pass")),
    )
    mode_recommendations = {}
    for mode, enabled in (("off", False), ("on", True)):
        mode_recommendations[f"preview_{mode}"] = _profile_lock_recommendation(
            [result for result in results if result.preview_enabled is enabled],
            verify_pixels,
            True,
            required_repeats,
            task_required,
            bool(task_quality.get("pass")),
        )
    summary["profile_lock_by_preview_mode"] = mode_recommendations
    original_summary_path = summary_path.with_name("validation_summary.acquisition.json")
    if not original_summary_path.exists():
        original_summary_path.write_text(
            summary_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    _atomic_write_json(summary_path, summary)
    status_path = summary_path.with_name("profile_status.json")
    status_base = {
        "schema_version": 1,
        "profile_id": config.recording_profile.id,
        "profile_version": config.recording_profile.version,
        "approved": False,
        "task_quality_status": str((task_quality.get("document") or {}).get("status") or "not_required"),
        "validation_summary": str(summary_path),
        "validation_summary_sha256": _sha256_file(summary_path),
        "task_quality_record": str(task_quality_record_path) if task_quality_record_path else "",
        "task_quality_record_sha256": str(task_quality.get("sha256") or ""),
        "preview_mode_recommendations": mode_recommendations,
        "approved_configs": {},
        "updated_utc": datetime.now(timezone.utc).isoformat(),
    }
    if task_required and not task_quality.get("pass"):
        status_base["state"] = (
            "task_quality_pending"
            if status_base["task_quality_status"] == "pending"
            else "task_quality_failed"
        )
        _atomic_write_json(status_path, status_base)
        issues = "; ".join(str(issue) for issue in task_quality.get("issues") or [])
        raise ValueError(
            "Task-quality record was updated from the sweep but is not ready. Add the immutable "
            f"reference artifact and real task metrics, then finalize again. {issues}"
        )
    for mode in ("off", "on"):
        recommendation = mode_recommendations[f"preview_{mode}"]
        if not str(recommendation).startswith("lock_validated_for_this_evidence_fingerprint_"):
            status_base["state"] = "not_lockable"
            _atomic_write_json(status_path, status_base)
            raise ValueError(
                f"Cannot create both approved configs: preview {mode} is not lockable ({recommendation})."
            )

    approved_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        mode: approved_dir / f"{config_path.stem}_preview_{mode}_approved.yaml"
        for mode in ("off", "on")
    }
    existing = [path for path in outputs.values() if path.exists()]
    if existing:
        raise FileExistsError(
            "Refusing to overwrite approved config(s): " + ", ".join(str(path) for path in existing)
        )
    created: list[Path] = []
    lock_reports: dict[str, Any] = {}
    try:
        for mode in ("off", "on"):
            lock_reports[mode] = lock_profile_from_summary(
                config_path,
                summary_path,
                outputs[mode],
                preview_mode=mode,
            )
            created.append(outputs[mode])
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise

    status_document = {
        "schema_version": 1,
        "profile_id": config.recording_profile.id,
        "profile_version": config.recording_profile.version,
        "state": "approved",
        "approved": True,
        "task_quality_status": "pass" if task_required else "not_required",
        "validation_summary": str(summary_path),
        "validation_summary_sha256": _sha256_file(summary_path),
        "task_quality_record": str(task_quality_record_path.expanduser().resolve()) if task_quality_record_path else "",
        "task_quality_record_sha256": str(task_quality.get("sha256") or ""),
        "preview_mode_recommendations": mode_recommendations,
        "approved_configs": {mode: str(path) for mode, path in outputs.items()},
        "updated_utc": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(status_path, status_document)
    return {
        "profile_state": "approved",
        "profile_status_path": str(status_path),
        "validation_summary_path": str(summary_path),
        "task_quality_record_path": str(task_quality_record_path) if task_quality_record_path else "",
        "approved_configs": {mode: str(path) for mode, path in outputs.items()},
        "locks": lock_reports,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
