"""Hardware fingerprint helpers for validation evidence."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __release_stage__, __version__


def build_hardware_fingerprint(
    *,
    camera_config: dict[str, Any] | None = None,
    device_info: dict[str, Any] | None = None,
    pfs_sha256: str | None = None,
    ffmpeg_path: str | None = None,
    ffprobe_path: str | None = None,
    ffmpeg_version: str | None = None,
) -> dict[str, Any]:
    """Return a stable-ish evidence fingerprint for local validation.

    The hash intentionally includes the computer and transport stack as well as
    the camera. A profile validated on one host should be revalidated after a
    camera, frame grabber, driver, Python environment, or GPU change.
    """

    payload: dict[str, Any] = {
        "schema_version": 2,
        "host": {
            "computer_name": socket.gethostname(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python": sys.version,
            "executable": sys.executable,
        },
        "environment": {
            "genicam_gentl64_path": os.environ.get("GENICAM_GENTL64_PATH", ""),
            "genicam_gentl32_path": os.environ.get("GENICAM_GENTL32_PATH", ""),
            "pypylon_version": _pypylon_version(),
            "ffmpeg_path": ffmpeg_path or "",
            "ffprobe_path": ffprobe_path or "",
            "ffmpeg_version": ffmpeg_version or "",
        },
        "camera": {
            "make": _nested_value(camera_config or {}, ["make"]),
            "serial": _nested_value(camera_config or {}, ["serial"]) or (device_info or {}).get("serial"),
            "model": (device_info or {}).get("model"),
            "vendor": (device_info or {}).get("vendor"),
            "device_class": (device_info or {}).get("device_class"),
            "interface_id": (device_info or {}).get("interface_id"),
            "device_factory": (device_info or {}).get("device_factory"),
            "width": _nested_value(camera_config or {}, ["expected_width"]) or (device_info or {}).get("width"),
            "height": _nested_value(camera_config or {}, ["expected_height"]) or (device_info or {}).get("height"),
            "pixel_format": _nested_value(camera_config or {}, ["expected_pixel_format"]) or (device_info or {}).get("pixel_format"),
            "fps": _nested_value(camera_config or {}, ["expected_fps"]) or (device_info or {}).get("acquisition_frame_rate"),
            "pfs_sha256": pfs_sha256 or "",
        },
        "gpu": _gpu_summary(),
        "software": {
            "pycamrec_version": __version__,
            "release_stage": __release_stage__,
            "git_commit": _git_value("rev-parse", "HEAD"),
            "git_dirty": _git_dirty(),
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "schema_version": 2,
        "fingerprint_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "payload": payload,
    }


def session_hardware_fingerprint(session_dir: Path) -> dict[str, Any]:
    session_path = session_dir / "session.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    existing = session.get("hardware_fingerprint")
    if isinstance(existing, dict) and existing.get("fingerprint_sha256"):
        return existing
    resolved = session.get("resolved") if isinstance(session.get("resolved"), dict) else {}
    return build_hardware_fingerprint(
        camera_config=resolved.get("camera") if isinstance(resolved.get("camera"), dict) else {},
        device_info=session.get("device_info") if isinstance(session.get("device_info"), dict) else {},
        pfs_sha256=str(session.get("pfs_sha256_verified") or ""),
        ffmpeg_path=_nested_value(resolved, ["writer", "ffmpeg_path"]),
        ffprobe_path=_nested_value(resolved, ["writer", "ffprobe_path"]),
        ffmpeg_version=_nested_value(session, ["preflight", "ffmpeg_version"]),
    )


def build_profile_fingerprint(
    *,
    camera_config: dict[str, Any],
    writer_config: dict[str, Any],
    recording_profile: dict[str, Any],
    preview_config: dict[str, Any],
) -> dict[str, Any]:
    """Hash every resolved acquisition/encoding setting covered by a profile approval."""

    stable_preview = dict(preview_config)
    # Runtime shared-memory/file locations identify one GUI process, not profile behavior.
    stable_preview.pop("image_path", None)
    stable_preview.pop("window_title", None)
    if not bool(stable_preview.get("enabled")):
        stable_preview = {"enabled": False}
    payload = {
        "schema_version": 1,
        "camera": _jsonable(camera_config),
        "writer": _jsonable(writer_config),
        "recording_profile": _jsonable(recording_profile),
        "preview": _jsonable(stable_preview),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "schema_version": 1,
        "fingerprint_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "payload": payload,
    }


def evaluate_profile_approval(
    approval_config: dict[str, Any] | None,
    *,
    evidence_fingerprint_sha256: str,
    profile_fingerprint_sha256: str,
    preview_enabled: bool,
    duration_s: float,
) -> dict[str, Any]:
    """Evaluate a configured lock against the exact current evidence fingerprint."""

    approval = approval_config if isinstance(approval_config, dict) else {}
    status = str(approval.get("status") or "requires_hardware_validation")
    locked = status.startswith("locked")
    configured_fingerprint = str(approval.get("evidence_fingerprint_sha256") or "")
    configured_profile_fingerprint = str(approval.get("profile_fingerprint_sha256") or "")
    validated_max_duration_s = _optional_float(approval.get("validated_max_duration_s"))
    intended_preview_mode = str(approval.get("intended_preview_mode") or "off").strip().lower()
    actual_preview_mode = "on" if preview_enabled else "off"
    fingerprint_match = bool(configured_fingerprint) and configured_fingerprint == evidence_fingerprint_sha256
    profile_fingerprint_match = (
        bool(configured_profile_fingerprint)
        and configured_profile_fingerprint == profile_fingerprint_sha256
    )
    preview_mode_match = intended_preview_mode == actual_preview_mode
    duration_match = validated_max_duration_s is not None and duration_s <= validated_max_duration_s
    approved = locked and fingerprint_match and profile_fingerprint_match and preview_mode_match and duration_match
    reasons: list[str] = []
    if not locked:
        reasons.append("profile status is not locked")
    if not configured_fingerprint:
        reasons.append("approved evidence fingerprint is missing")
    elif not fingerprint_match:
        reasons.append("camera/PFS/GPU/driver/host/software evidence fingerprint changed")
    if not configured_profile_fingerprint:
        reasons.append("approved resolved profile fingerprint is missing")
    elif not profile_fingerprint_match:
        reasons.append("camera/writer/profile/preview settings changed")
    if not preview_mode_match:
        reasons.append(
            f"intended preview mode is {intended_preview_mode!r}, current mode is {actual_preview_mode!r}"
        )
    if validated_max_duration_s is None:
        reasons.append("validated maximum duration is missing")
    elif not duration_match:
        reasons.append(
            f"requested duration {duration_s:g}s exceeds validated maximum {validated_max_duration_s:g}s"
        )
    return {
        "status": status,
        "locked": locked,
        "approved": approved,
        "configured_evidence_fingerprint_sha256": configured_fingerprint,
        "current_evidence_fingerprint_sha256": evidence_fingerprint_sha256,
        "fingerprint_match": fingerprint_match,
        "configured_profile_fingerprint_sha256": configured_profile_fingerprint,
        "current_profile_fingerprint_sha256": profile_fingerprint_sha256,
        "profile_fingerprint_match": profile_fingerprint_match,
        "intended_preview_mode": intended_preview_mode,
        "actual_preview_mode": actual_preview_mode,
        "preview_mode_match": preview_mode_match,
        "validated_max_duration_s": validated_max_duration_s,
        "requested_duration_s": duration_s,
        "duration_match": duration_match,
        "reasons": reasons,
    }


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _pypylon_version() -> str:
    try:
        return importlib.metadata.version("pypylon")
    except importlib.metadata.PackageNotFoundError:
        try:
            import pypylon.pylon as pylon
        except Exception:
            return ""
        return str(getattr(pylon, "__version__", ""))


def _gpu_summary() -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version",
                "--format=csv,noheader",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return {"nvidia_smi": ""}
    return {"nvidia_smi": completed.stdout.strip()}


def _nested_value(data: dict[str, Any], path: list[str]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _git_value(*args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(Path(__file__).resolve().parents[1]), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    return completed.stdout.strip()


def _git_dirty() -> bool | None:
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                "safe.directory=*",
                "-C",
                str(Path(__file__).resolve().parents[1]),
                "status",
                "--porcelain",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    return bool(completed.stdout.strip())
