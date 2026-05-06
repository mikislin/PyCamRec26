"""Preflight checks for camera recording sessions."""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .schemas import PyCamRecConfig


@dataclass(frozen=True)
class PreflightReport:
    pfs_sha256: str
    pfs_features: dict[str, str]
    raw_bytes_per_second: float
    raw_bytes_total: float
    output_free_bytes: int
    ffmpeg_version: str
    warnings: tuple[str, ...]


def run_preflight(cfg: PyCamRecConfig) -> PreflightReport:
    output_root = cfg.session.output_root
    output_root.mkdir(parents=True, exist_ok=True)

    pfs_sha256 = sha256_file(cfg.camera.pfs_path)
    pfs_features = parse_pfs_features(cfg.camera.pfs_path)
    raw_bps = cfg.frame_bytes * cfg.camera.expected_fps
    raw_total = raw_bps * cfg.session.duration_s
    disk_usage = shutil.disk_usage(output_root)
    ffmpeg_version = probe_ffmpeg(cfg.writer.ffmpeg_path)

    warnings: list[str] = []
    missing_experiment_fields = cfg.experiment.missing_fields()
    if missing_experiment_fields:
        warnings.append(
            "Experiment metadata has UNSPECIFIED fields: "
            + ", ".join(missing_experiment_fields)
            + ". Fill the experiment section before scientific recording."
        )
    min_free_bytes = cfg.writer.min_free_space_gb * 1024**3
    if disk_usage.free < min_free_bytes:
        warnings.append(
            f"Free disk space is below configured minimum free-space guard "
            f"({cfg.writer.min_free_space_gb:.1f} GiB)."
        )
    if disk_usage.free < raw_total:
        warnings.append(
            "Free disk space is below estimated raw payload size. "
            "Compressed recording may still fit, but raw recording will not."
        )
    if cfg.writer.mode == "raw_chunked" and disk_usage.free < raw_total * 1.10:
        warnings.append("raw_chunked mode needs at least 10% more free space than the raw payload.")
    if cfg.writer.mode == "compressed_nvenc" and "nvenc" not in cfg.writer.codec.lower():
        warnings.append("writer.mode is compressed_nvenc but writer.codec is not an NVENC codec.")
    if cfg.writer.queue_max_frames * cfg.frame_bytes > 8 * 1024**3:
        warnings.append("Queue memory limit exceeds 8 GiB; use a smaller queue unless RAM has been reserved.")
    if cfg.writer.hash_segments and cfg.writer.segment_seconds < cfg.session.duration_s:
        warnings.append("writer.hash_segments is enabled during segmented recording; hashing can stall acquisition.")
    if cfg.writer.spool_output:
        warnings.append(
            "writer.spool_output is enabled: encoded bytes are buffered in RAM before/during disk writes. "
            f"Maximum RAM spool is {cfg.writer.spool_max_bytes / 1024**3:.1f} GiB."
        )
    if cfg.preview.enabled and cfg.preview.sink in {"window", "file"} and importlib.util.find_spec("cv2") is None:
        warnings.append("Preview is enabled but OpenCV (cv2) is not installed; recording will continue without preview.")
    if cfg.writer.expected_bitrate_mbps is not None:
        estimated_bytes = cfg.writer.expected_bitrate_mbps * 1_000_000 / 8 * cfg.session.duration_s
        if disk_usage.free < estimated_bytes * 1.20:
            warnings.append(
                "Free disk space is below 120% of the expected profile output size "
                f"({estimated_bytes / 1024**3:.1f} GiB estimated)."
            )
    if (
        cfg.recording_profile.max_duration_s is not None
        and cfg.session.duration_s > cfg.recording_profile.max_duration_s
        and not cfg.recording_profile.enforce_max_duration
    ):
        warnings.append(
            f"Recording profile {cfg.recording_profile.id!r} is recommended for "
            f"runs up to {cfg.recording_profile.max_duration_s:g} seconds; "
            f"this config requests {cfg.session.duration_s:g} seconds."
        )
    if (
        cfg.recording_profile.tested_duration_s is not None
        and cfg.session.duration_s > cfg.recording_profile.tested_duration_s
    ):
        warnings.append(
            f"Recording profile {cfg.recording_profile.id!r} has been validated for "
            f"{cfg.recording_profile.tested_duration_s:g} seconds on this system; "
            f"this config requests {cfg.session.duration_s:g} seconds."
        )
    if cfg.recording_profile.pixel_fidelity == "lossless" and cfg.session.duration_s > 120:
        warnings.append("Lossless profile runs longer than 2 minutes are not recommended on this hardware.")
    if (
        cfg.recording_profile.id != "custom"
        and not cfg.recording_profile.validation_status.startswith("validated_")
    ):
        warnings.append(
            f"Recording profile {cfg.recording_profile.id!r} has status "
            f"{cfg.recording_profile.validation_status!r}; validate with a short run before scientific use."
        )
    warnings.extend(_pfs_warnings(cfg, pfs_features))

    return PreflightReport(
        pfs_sha256=pfs_sha256,
        pfs_features=pfs_features,
        raw_bytes_per_second=raw_bps,
        raw_bytes_total=raw_total,
        output_free_bytes=disk_usage.free,
        ffmpeg_version=ffmpeg_version,
        warnings=tuple(warnings),
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_ffmpeg(ffmpeg_path: str) -> str:
    completed = subprocess.run(
        [ffmpeg_path, "-version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    first_line = completed.stdout.splitlines()[0] if completed.stdout else ""
    return first_line.strip()


def parse_pfs_features(path: Path) -> dict[str, str]:
    wanted = {
        "Width",
        "Height",
        "PixelFormat",
        "AcquisitionFrameRate",
        "AcquisitionFrameRateEnable",
        "ExposureTime",
        "Gain",
        "GainAuto",
        "ExposureAuto",
        "LUTEnable",
        "ChunkModeActive",
    }
    features: dict[str, str] = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split(None, 1)
            if len(parts) == 2 and parts[0] in wanted:
                features[parts[0]] = parts[1].strip()
    return features


def _pfs_warnings(cfg: PyCamRecConfig, features: dict[str, str]) -> list[str]:
    warnings: list[str] = []
    comparisons = {
        "Width": str(cfg.camera.expected_width),
        "Height": str(cfg.camera.expected_height),
        "PixelFormat": cfg.camera.expected_pixel_format,
    }
    for key, expected in comparisons.items():
        actual = features.get(key)
        if actual is not None and str(actual) != expected:
            warnings.append(f"PFS {key}={actual!r} does not match config expected {expected!r}.")

    actual_fps = features.get("AcquisitionFrameRate")
    if actual_fps is not None:
        try:
            fps_mismatch = abs(float(actual_fps) - cfg.camera.expected_fps) > 0.01
        except ValueError:
            fps_mismatch = True
        if fps_mismatch:
            warnings.append(
                f"PFS AcquisitionFrameRate={actual_fps!r} does not match config expected {cfg.camera.expected_fps!r}."
            )
    if features.get("LUTEnable") == "1":
        warnings.append("PFS has LUTEnable=1. Confirm it is an identity LUT or disable it for quantitative imaging.")
    if features.get("ChunkModeActive") in {None, "0"} and cfg.camera.enable_chunks:
        warnings.append("PFS does not enable chunk mode; PyCamRec will attempt to enable selected chunks at runtime.")
    return warnings
