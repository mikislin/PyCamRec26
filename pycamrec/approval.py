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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
