"""Create hardware-specific approved configs from validation evidence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import load_config


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
    fingerprints = {
        str(result.get("hardware_fingerprint_sha256") or "")
        for result in candidates
        if result.get("hardware_fingerprint_sha256")
    }
    if len(fingerprints) != 1:
        raise ValueError("Lock candidates have mixed or missing evidence fingerprints.")
    fingerprint = next(iter(fingerprints))
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
        "validation_summary_path": str(summary_path),
        "locked_utc": datetime.now(timezone.utc).isoformat(),
        "recommendation": recommendation,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return {
        "output_path": str(output_path),
        "profile_id": config.recording_profile.id,
        "preview_mode": preview_mode,
        "evidence_fingerprint_sha256": fingerprint,
        "validation_summary_path": str(summary_path),
    }
