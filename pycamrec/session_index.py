"""Portable discovery and tabular indexing of PyCamRec sessions."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def discover_session_dirs(output_root: Path) -> list[Path]:
    root = output_root.expanduser().resolve()
    if not root.exists():
        return []
    sessions = {path.parent for path in root.rglob("session.json") if path.is_file()}
    return sorted(sessions, key=lambda path: path.stat().st_mtime, reverse=True)


def build_session_index(output_root: Path, *, include_qc: bool = False) -> list[dict[str, Any]]:
    rows = []
    for session_dir in discover_session_dirs(output_root):
        rows.append(_session_row(output_root.expanduser().resolve(), session_dir, include_qc=include_qc))
    return rows


def write_session_index(
    output_root: Path,
    output_path: Path,
    *,
    output_format: str = "csv",
    include_qc: bool = False,
) -> dict[str, Any]:
    rows = build_session_index(output_root, include_qc=include_qc)
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "jsonl":
        with output_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    elif output_format == "csv":
        fields = _ordered_fields(rows)
        with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    else:
        raise ValueError("output_format must be 'csv' or 'jsonl'.")
    return {
        "schema_version": 1,
        "output_root": str(output_root.expanduser().resolve()),
        "output_path": str(output_path),
        "format": output_format,
        "session_count": len(rows),
        "include_qc": include_qc,
    }


def _session_row(output_root: Path, session_dir: Path, *, include_qc: bool) -> dict[str, Any]:
    session = _read_json(session_dir / "session.json")
    metadata = _read_json(session_dir / "experiment_metadata.json")
    fixed = metadata.get("fixed_fields") or {}
    project = metadata.get("project") or {
        "project_id": fixed.get("project_id") or fixed.get("project_protocol"),
        "protocol_id": fixed.get("protocol_id") or fixed.get("project_protocol"),
        "assay_id": fixed.get("assay_id") or fixed.get("test_assay_name"),
    }
    subject = metadata.get("subject") or {
        "subject_id": fixed.get("subject_id") or fixed.get("animal_id"),
        "species": fixed.get("species"),
        "date_of_birth": fixed.get("date_of_birth") or fixed.get("dob"),
        "postnatal_day": fixed.get("postnatal_day"),
        "weight_g": fixed.get("weight_g"),
        "genotype": fixed.get("genotype"),
        "experimental_group": fixed.get("experimental_group"),
        "sex": fixed.get("sex"),
    }
    acquisition = metadata.get("acquisition") or {
        "experimenter_id": fixed.get("experimenter_id") or fixed.get("experimentator"),
        "run_index": fixed.get("run_index"),
    }
    resolved = session.get("resolved") or {}
    camera = resolved.get("camera") or {}
    profile = resolved.get("recording_profile") or {}
    try:
        relative_path = str(session_dir.relative_to(output_root))
    except ValueError:
        relative_path = session_dir.name
    row: dict[str, Any] = {
        "session_id": session.get("session_id") or metadata.get("automatic", {}).get("session_id") or session_dir.name,
        "created_utc": session.get("created_utc"),
        "relative_session_path": relative_path,
        "naming_schema_version": session.get("naming_schema_version")
        or metadata.get("automatic", {}).get("naming_schema_version"),
        "metadata_schema_version": metadata.get("schema_version"),
        "metadata_complete": metadata.get("metadata_complete"),
        "project_id": project.get("project_id"),
        "protocol_id": project.get("protocol_id"),
        "assay_id": project.get("assay_id"),
        "subject_id": subject.get("subject_id"),
        "species": subject.get("species"),
        "date_of_birth": subject.get("date_of_birth"),
        "postnatal_day": subject.get("postnatal_day"),
        "weight_g": subject.get("weight_g"),
        "genotype": subject.get("genotype"),
        "experimental_group": subject.get("experimental_group"),
        "sex": subject.get("sex"),
        "experimenter_id": acquisition.get("experimenter_id"),
        "run_index": acquisition.get("run_index"),
        "camera_serial": camera.get("serial"),
        "camera_width": camera.get("expected_width"),
        "camera_height": camera.get("expected_height"),
        "camera_fps": camera.get("expected_fps"),
        "camera_pixel_format": camera.get("expected_pixel_format"),
        "profile_id": profile.get("id"),
        "profile_pixel_fidelity": profile.get("pixel_fidelity"),
    }
    for key, item in (metadata.get("custom_fields") or fixed.get("custom_fields") or {}).items():
        row[f"custom__{key}"] = item.get("value") if isinstance(item, dict) else item
    if include_qc:
        from .report import build_session_report

        qc = build_session_report(session_dir).get("qc") or {}
        row.update(
            {
                "qc_status": qc.get("status"),
                "acquisition_pass": qc.get("acquisition_pass"),
                "qc_pass": qc.get("qc_pass"),
                "health_pass": qc.get("health_pass"),
                "profile_approval_pass": qc.get("profile_approval_pass"),
                "experiment_ready": qc.get("experiment_ready"),
            }
        )
    return row


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _ordered_fields(rows: list[dict[str, Any]]) -> list[str]:
    preferred = [
        "session_id",
        "created_utc",
        "relative_session_path",
        "project_id",
        "protocol_id",
        "assay_id",
        "subject_id",
        "postnatal_day",
        "weight_g",
        "profile_id",
        "qc_status",
        "experiment_ready",
    ]
    available = {key for row in rows for key in row}
    return [key for key in preferred if key in available] + sorted(available - set(preferred))
