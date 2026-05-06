"""Built-in recording profiles for PyCamRec."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


PROFILE_VERSION = "2026-05-04"


@dataclass(frozen=True)
class RecordingProfileDefinition:
    id: str
    display_name: str
    version: str
    compression: str
    pixel_fidelity: str
    validation_status: str
    tested_duration_s: float | None
    max_duration_s: float | None
    enforce_max_duration: bool
    expected_bitrate_mbps: float | None
    description: str
    recommended_use: str
    writer_defaults: dict[str, Any]

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("writer_defaults", None)
        return data


LONG_LOSSY_H264_250M = RecordingProfileDefinition(
    id="long_lossy_h264_250m",
    display_name="Long nonstop lossy H.264 250 Mbps",
    version=PROFILE_VERSION,
    compression="lossy_h264_nvenc",
    pixel_fidelity="lossy",
    validation_status="validated_2h_on_current_system",
    tested_duration_s=7200.0,
    max_duration_s=None,
    enforce_max_duration=False,
    expected_bitrate_mbps=250.0,
    description=(
        "Validated full-frame 200 fps profile for long nonstop recordings. "
        "Frame continuity and timestamps are preserved; pixel values are H.264 lossy compressed."
    ),
    recommended_use="Long behavioral recordings where frame count and timing matter more than exact pixel intensity.",
    writer_defaults={
        "mode": "compressed_nvenc",
        "segment_seconds": 120,
        "input_pix_fmt": "gray",
        "codec": "h264_nvenc",
        "container": "avi",
        "output_pix_fmt": "yuv420p",
        "queue_max_frames": 512,
        "overwrite": False,
        "hash_segments": False,
        "expected_bitrate_mbps": 250,
        "min_free_space_gb": 10,
        "output_args": (
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p1",
            "-tune",
            "ull",
            "-rc",
            "cbr",
            "-b:v",
            "250M",
            "-maxrate",
            "250M",
            "-bufsize",
            "125M",
            "-bf",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "pc",
        ),
    },
)


NEAR_LOSSLESS_H264_400M = RecordingProfileDefinition(
    id="near_lossless_h264_400m",
    display_name="Near-lossless H.264 NVENC 400 Mbps",
    version=PROFILE_VERSION,
    compression="near_lossless_h264_nvenc",
    pixel_fidelity="near_lossless_lossy",
    validation_status="validated_10min_on_current_system",
    tested_duration_s=600.0,
    max_duration_s=1800.0,
    enforce_max_duration=False,
    expected_bitrate_mbps=400.0,
    description=(
        "High-quality capped-bitrate NVENC profile for sustained 10-30 minute full-frame recordings. "
        "Validated at 10 minutes on the current system. This replaces the old CQP8 stress profile, "
        "which produced uncontrolled 1+ Gbps bursts and failed real-time timing."
    ),
    recommended_use=(
        "Highest-quality practical experiment profile to validate after the proven 250 Mbps long-lossy mode. "
        "Use when visual detail matters more than file size, but exact pixel values are not required."
    ),
    writer_defaults={
        "mode": "compressed_nvenc",
        "segment_seconds": 120,
        "input_pix_fmt": "gray",
        "codec": "h264_nvenc",
        "container": "avi",
        "output_pix_fmt": "yuv420p",
        "queue_max_frames": 1024,
        "overwrite": False,
        "hash_segments": False,
        "expected_bitrate_mbps": 400,
        "min_free_space_gb": 100,
        "output_args": (
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p1",
            "-tune",
            "ull",
            "-rc",
            "cbr",
            "-b:v",
            "400M",
            "-maxrate",
            "400M",
            "-bufsize",
            "200M",
            "-bf",
            "0",
            "-g",
            "1200",
            "-surfaces",
            "64",
            "-zerolatency",
            "1",
            "-gpu",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "pc",
        ),
    },
)


LOSSLESS_H264_NVENC_GPU = RecordingProfileDefinition(
    id="lossless_h264_nvenc_gpu",
    display_name="GPU lossless H.264 NVENC gray calibration",
    version=PROFILE_VERSION,
    compression="lossless_h264_nvenc",
    pixel_fidelity="lossless",
    validation_status="validated_30s_on_current_system",
    tested_duration_s=30.0,
    max_duration_s=30.0,
    enforce_max_duration=False,
    expected_bitrate_mbps=3600.0,
    description=(
        "GPU-backed NVENC lossless H.264 profile using the yuv420p luma path for Mono8 data. "
        "Encoded AVI bytes are drained through a bounded RAM spool so slow disk writes do not immediately stall capture. "
        "Synthetic full-size gray frames round-trip exactly. Camera validation passed at 30 seconds; 60 seconds failed real-time timing."
    ),
    recommended_use="Short lossless calibration tests up to 30 seconds where exact decoded grayscale pixels must be verified after recording.",
    writer_defaults={
        "mode": "lossless_nvenc_spooled",
        "segment_seconds": 120,
        "input_pix_fmt": "gray",
        "codec": "h264_nvenc",
        "container": "avi",
        "output_pix_fmt": "yuv420p",
        "queue_max_frames": 1024,
        "overwrite": False,
        "hash_segments": False,
        "expected_bitrate_mbps": 3600,
        "min_free_space_gb": 80,
        "spool_output": True,
        "spool_chunk_bytes": 8388608,
        "spool_max_bytes": 34359738368,
        "output_args": (
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p1",
            "-tune",
            "lossless",
            "-profile:v",
            "high",
            "-rc",
            "constqp",
            "-qp",
            "0",
            "-bf",
            "0",
            "-g",
            "1200",
            "-surfaces",
            "64",
            "-zerolatency",
            "1",
            "-gpu",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "pc",
        ),
    },
)


PROFILE_LIBRARY: dict[str, RecordingProfileDefinition] = {
    profile.id: profile
    for profile in (
        LONG_LOSSY_H264_250M,
        NEAR_LOSSLESS_H264_400M,
        LOSSLESS_H264_NVENC_GPU,
    )
}


def get_profile(profile_id: str) -> RecordingProfileDefinition:
    try:
        return PROFILE_LIBRARY[profile_id]
    except KeyError as exc:
        choices = ", ".join(sorted(PROFILE_LIBRARY))
        raise ValueError(f"Unknown recording profile {profile_id!r}. Available profiles: {choices}") from exc


def list_profile_dicts() -> list[dict[str, Any]]:
    return [PROFILE_LIBRARY[key].public_dict() for key in sorted(PROFILE_LIBRARY)]


def profile_config_dict(
    profile: RecordingProfileDefinition,
    enforce_max_duration: bool | None = None,
) -> dict[str, Any]:
    data = profile.public_dict()
    if enforce_max_duration is not None:
        data["enforce_max_duration"] = enforce_max_duration
    return data
