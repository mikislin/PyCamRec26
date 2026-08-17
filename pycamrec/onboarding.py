"""Camera onboarding helpers for generated configs and evidence review."""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import yaml

from .config import load_config
from .pixel_formats import ffmpeg_raw_pix_fmt, is_bayer_pixel_format, is_mono_pixel_format, is_rgb_pixel_format
from .preflight import parse_pfs_features
from .profiles import get_profile


GENERATED_CONFIG_DIR = Path("configs") / "generated"


@dataclass(frozen=True)
class GeneratedConfigReport:
    path: str
    profile: str
    camera_make: str
    serial: str
    pfs_path: str
    expected_width: int
    expected_height: int
    pixel_format: str
    expected_fps: float
    segment_seconds: float
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def generate_camera_config(
    base_config: Path,
    *,
    output_path: Path | None = None,
    output_dir: Path | None = None,
    profile: str | None = None,
    camera_make: str | None = None,
    serial: str | None = None,
    pfs_path: Path | None = None,
    pixel_format: str | None = None,
    expected_fps: float | None = None,
    width: int | None = None,
    height: int | None = None,
    segment_seconds: float = 120.0,
    runtime_pixel_format_override: bool = True,
    runtime_frame_rate_override: bool = False,
) -> GeneratedConfigReport:
    """Generate a reviewable candidate YAML for a camera/pixel-format choice."""

    base_config = base_config.expanduser().resolve()
    data = yaml.safe_load(base_config.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a mapping: {base_config}")

    base_cfg = load_config(base_config, duration_s=1.0)
    camera = dict(data.get("camera", {}))
    writer = dict(data.get("writer", {}))
    session = dict(data.get("session", {}))

    resolved_pixel_format = str(pixel_format or camera.get("expected_pixel_format") or base_cfg.camera.expected_pixel_format)
    resolved_make = str(camera_make or camera.get("make") or base_cfg.camera.make)
    resolved_width = int(width if width is not None else camera.get("expected_width", base_cfg.camera.expected_width))
    resolved_profile = profile or _default_profile_for_camera(
        resolved_pixel_format,
        camera_make=resolved_make,
        width=resolved_width,
    )
    resolved_fps = float(expected_fps if expected_fps is not None else camera.get("expected_fps", base_cfg.camera.expected_fps))
    resolved_segment_seconds = float(segment_seconds)
    if resolved_segment_seconds <= 0:
        raise ValueError("segment_seconds must be positive.")

    camera["make"] = resolved_make
    camera["serial"] = str(serial or camera.get("serial") or base_cfg.camera.serial)
    if pfs_path is not None:
        camera["pfs_path"] = str(pfs_path.expanduser().resolve())
    else:
        existing_pfs = Path(str(camera.get("pfs_path") or base_cfg.camera.pfs_path)).expanduser()
        if not existing_pfs.is_absolute():
            existing_pfs = (base_config.parent / existing_pfs).resolve()
        camera["pfs_path"] = str(existing_pfs)
    camera["expected_width"] = resolved_width
    camera["expected_height"] = int(height if height is not None else camera.get("expected_height", base_cfg.camera.expected_height))
    camera["expected_pixel_format"] = resolved_pixel_format
    camera["expected_fps"] = resolved_fps
    camera["allow_runtime_pixel_format_override"] = bool(runtime_pixel_format_override)
    camera["allow_runtime_frame_rate_override"] = bool(runtime_frame_rate_override)
    _validate_pfs_candidate(
        Path(str(camera["pfs_path"])),
        expected_serial=str(camera["serial"]),
        expected_width=int(camera["expected_width"]),
        expected_height=int(camera["expected_height"]),
        expected_pixel_format=resolved_pixel_format,
        expected_fps=resolved_fps,
        runtime_pixel_format_override=bool(runtime_pixel_format_override),
        runtime_frame_rate_override=bool(runtime_frame_rate_override),
    )

    writer["segment_seconds"] = resolved_segment_seconds
    writer["input_pix_fmt"] = ffmpeg_raw_pix_fmt(resolved_pixel_format)
    writer["expected_bitrate_mbps"] = get_profile(resolved_profile).expected_bitrate_mbps
    if is_rgb_pixel_format(resolved_pixel_format):
        writer["output_pix_fmt"] = "yuv420p"

    session["name"] = _session_name(camera, resolved_pixel_format, resolved_fps, resolved_profile)
    notes = _notes_for_pixel_format(resolved_pixel_format)
    session["notes"] = notes

    data["profile"] = resolved_profile
    data["session"] = session
    data["camera"] = camera
    data["writer"] = writer
    data["onboarding"] = {
        "schema_version": 2,
        "source_config": str(base_config),
        "status": "generated_unvalidated",
        "recommended_first_sweep": (
            "Start with full-frame lossless GPU recording with live preview enabled, "
            "then repeat preview off/on at longer durations. Treat any new camera or computer "
            "as requiring revalidation."
        ),
        "reliability_target": {
            "frame_count_equals_expected": True,
            "ffprobe_count_equals_expected": True,
            "block_id_gaps": 0,
            "observed_fps_min_ratio": 0.98,
            "max_queue_depth_fraction_fail": 0.90,
            "max_queue_depth_fraction_preferred": 0.25,
            "pixel_fidelity_verified": True,
            "metadata_complete_for_experiments": True,
        },
    }

    path = output_path.expanduser().resolve() if output_path is not None else _generated_path(
        output_dir=output_dir,
        camera=camera,
        pixel_format=resolved_pixel_format,
        fps=resolved_fps,
        profile=resolved_profile,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return GeneratedConfigReport(
        path=str(path),
        profile=resolved_profile,
        camera_make=str(camera["make"]),
        serial=str(camera["serial"]),
        pfs_path=str(camera["pfs_path"]),
        expected_width=int(camera["expected_width"]),
        expected_height=int(camera["expected_height"]),
        pixel_format=resolved_pixel_format,
        expected_fps=resolved_fps,
        segment_seconds=resolved_segment_seconds,
        notes=notes,
    )


def _default_profile_for_camera(pixel_format: str, *, camera_make: str, width: int) -> str:
    """Choose a transport-safe candidate without copying USB assumptions to CXP."""

    normalized_make = camera_make.strip().lower().replace("-", "_").replace(" ", "_")
    if is_mono_pixel_format(pixel_format):
        if normalized_make == "basler_cxp" or "cxp" in normalized_make or width > 2000:
            return "cxp_mono8_lossless_h264_nvenc_mp4"
        return "usb_mono8_lossless_h264_nvenc_mp4"
    if is_bayer_pixel_format(pixel_format):
        return "usb_bayer8_lossless_h264_nvenc_gpu"
    if is_rgb_pixel_format(pixel_format):
        return "analysis_h264_mp4_rgb_200m"
    raise ValueError(f"No safe default profile is known for pixel format {pixel_format!r}.")


def _notes_for_pixel_format(pixel_format: str) -> str:
    if is_mono_pixel_format(pixel_format):
        return (
            "Generated Mono8 candidate. This is the preferred CV-ready scientific path when color is not required: "
            "one byte per pixel, lossless GPU encoding, MP4 compatibility, and real-session pixel verification."
        )
    if is_bayer_pixel_format(pixel_format):
        return (
            "Generated raw Bayer candidate. This keeps one-byte raw mosaic payloads for color cameras. "
            "Use controlled offline debayer/export for color analysis; validate exact raw-byte fidelity before experiments."
        )
    if is_rgb_pixel_format(pixel_format):
        return (
            "Generated RGB/BGR candidate. Direct color triples the camera payload and current MP4 H.264 color storage "
            "is lossy/subsampled; use for engineering color convenience until a lossless color path is validated."
        )
    return "Generated candidate; validate before scientific use."


def _generated_path(
    *,
    output_dir: Path | None,
    camera: dict[str, Any],
    pixel_format: str,
    fps: float,
    profile: str,
) -> Path:
    root = (output_dir or GENERATED_CONFIG_DIR).expanduser()
    serial = _safe_token(str(camera.get("serial") or "camera"))
    make = _safe_token(str(camera.get("make") or "camera"))
    pixel = _safe_token(pixel_format)
    fps_text = _safe_token(f"{fps:g}fps")
    profile_text = _safe_token(profile)
    return (root / f"pycamrec_{make}_{serial}_{pixel}_{fps_text}_{profile_text}.yaml").resolve()


def _validate_pfs_candidate(
    pfs_path: Path,
    *,
    expected_serial: str,
    expected_width: int,
    expected_height: int,
    expected_pixel_format: str,
    expected_fps: float,
    runtime_pixel_format_override: bool,
    runtime_frame_rate_override: bool,
) -> None:
    features = parse_pfs_features(pfs_path)
    mismatches: list[str] = []
    if expected_serial:
        serial_match = re.search(r"\d{6,}", pfs_path.name)
        if serial_match and serial_match.group(0) != expected_serial:
            mismatches.append(
                f"PFS filename appears to belong to serial {serial_match.group(0)!r}, "
                f"but generated config expects serial {expected_serial!r}"
            )
    for key, expected in (("Width", expected_width), ("Height", expected_height)):
        actual = features.get(key)
        if actual is not None and not _numeric_text_matches(actual, float(expected)):
            mismatches.append(f"PFS {key}={actual!r} but generated config expects {expected!r}")
    actual_pixel_format = features.get("PixelFormat")
    if actual_pixel_format is not None and actual_pixel_format != expected_pixel_format:
        if not runtime_pixel_format_override:
            mismatches.append(
                f"PFS PixelFormat={actual_pixel_format!r} but generated config expects "
                f"{expected_pixel_format!r}"
            )
    actual_fps = features.get("AcquisitionFrameRate")
    if actual_fps is not None and not _numeric_text_matches(actual_fps, expected_fps):
        if not runtime_frame_rate_override:
            mismatches.append(
                f"PFS AcquisitionFrameRate={actual_fps!r} but generated config expects "
                f"{expected_fps!r}; enable runtime frame-rate override or choose a matching PFS/FPS"
            )
    if mismatches:
        raise ValueError(
            "Selected PFS is not compatible with the generated camera config. "
            + "; ".join(mismatches)
        )


def _numeric_text_matches(actual: str, expected: float, *, tolerance: float = 0.01) -> bool:
    try:
        return abs(float(actual) - float(expected)) <= tolerance
    except (TypeError, ValueError):
        return str(actual) == str(expected)


def _session_name(camera: dict[str, Any], pixel_format: str, fps: float, profile: str) -> str:
    serial = _safe_token(str(camera.get("serial") or "camera"))
    pixel = _safe_token(pixel_format)
    return f"{serial}_{pixel}_{fps:g}fps_{_safe_token(profile)}"


def _safe_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "value"
