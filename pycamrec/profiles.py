"""Built-in recording profiles for PyCamRec.

Profiles describe encoder intent only. Hardware approval is deliberately kept in
validation evidence because a camera, PFS, transport, GPU/driver, host, or
software change invalidates an earlier result.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


PROFILE_VERSION = "0.2.0rc2-2026-08-14"
REQUIRES_HARDWARE_VALIDATION = "requires_hardware_validation"


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


def _cbr_writer_defaults(bitrate_mbps: float, *, gop_frames: int) -> dict[str, Any]:
    bitrate = f"{bitrate_mbps:g}M"
    return {
        "mode": "compressed_nvenc",
        "segment_seconds": 120,
        "input_pix_fmt": "gray",
        "codec": "h264_nvenc",
        "container": "mp4",
        "output_pix_fmt": "yuv420p",
        "queue_max_frames": 1024,
        "overwrite": False,
        "hash_segments": False,
        "expected_bitrate_mbps": bitrate_mbps,
        "min_free_space_gb": 50,
        "finalize_timeout_s": 300,
        "output_args": (
            "-c:v", "h264_nvenc",
            "-preset", "p1",
            "-tune", "ull",
            "-rc", "cbr",
            "-b:v", bitrate,
            "-maxrate", bitrate,
            "-bufsize", f"{max(1.0, bitrate_mbps / 2):g}M",
            "-bf", "0",
            "-g", str(gop_frames),
            "-surfaces", "64",
            "-zerolatency", "1",
            "-gpu", "0",
            "-pix_fmt", "yuv420p",
            "-color_range", "pc",
        ),
    }


def _lossless_writer_defaults(
    expected_bitrate_mbps: float,
    *,
    segment_seconds: float,
    gop_frames: int,
) -> dict[str, Any]:
    return {
        "mode": "compressed_nvenc",
        "segment_seconds": segment_seconds,
        "input_pix_fmt": "gray",
        "codec": "h264_nvenc",
        "container": "mp4",
        "output_pix_fmt": "yuv420p",
        "queue_max_frames": 1024,
        "overwrite": False,
        "hash_segments": False,
        "expected_bitrate_mbps": expected_bitrate_mbps,
        "min_free_space_gb": 80,
        "finalize_timeout_s": 300,
        "output_args": (
            "-c:v", "h264_nvenc",
            "-preset", "p1",
            "-tune", "lossless",
            "-profile:v", "high",
            "-rc", "constqp",
            "-qp", "0",
            "-bf", "0",
            "-g", str(gop_frames),
            "-surfaces", "64",
            "-zerolatency", "1",
            "-gpu", "0",
            "-pix_fmt", "yuv420p",
            "-color_range", "pc",
        ),
    }


LONG_LOSSY_H264_250M = RecordingProfileDefinition(
    id="long_lossy_h264_250m",
    display_name="Legacy long H.264 NVENC 250 Mbps MP4",
    version=PROFILE_VERSION,
    compression="lossy_h264_nvenc",
    pixel_fidelity="lossy",
    validation_status=REQUIRES_HARDWARE_VALIDATION,
    tested_duration_s=None,
    max_duration_s=None,
    enforce_max_duration=False,
    expected_bitrate_mbps=250.0,
    description="Capped-bitrate Mono8-derived MP4. Frame timing can be exact while pixel values remain lossy.",
    recommended_use="Legacy long-run preset; prefer the camera-specific CV-optimal default config.",
    writer_defaults=_cbr_writer_defaults(250.0, gop_frames=1200),
)


NEAR_LOSSLESS_H264_400M = RecordingProfileDefinition(
    id="near_lossless_h264_400m",
    display_name="Near-lossless H.264 NVENC 400 Mbps MP4",
    version=PROFILE_VERSION,
    compression="near_lossless_h264_nvenc",
    pixel_fidelity="near_lossless_lossy",
    validation_status=REQUIRES_HARDWARE_VALIDATION,
    tested_duration_s=None,
    max_duration_s=None,
    enforce_max_duration=False,
    expected_bitrate_mbps=400.0,
    description="High-quality capped-bitrate MP4 that avoids uncontrolled lossless bursts but is not pixel exact.",
    recommended_use="High-detail computer-vision work that tolerates small compression error.",
    writer_defaults=_cbr_writer_defaults(400.0, gop_frames=1200),
)


# Retained for old configs. New CXP work should use the direct-MP4 camera-specific profile below.
LOSSLESS_H264_NVENC_GPU = RecordingProfileDefinition(
    id="lossless_h264_nvenc_gpu",
    display_name="Legacy lossless H.264 NVENC gray calibration",
    version=PROFILE_VERSION,
    compression="lossless_h264_nvenc",
    pixel_fidelity="lossless",
    validation_status=REQUIRES_HARDWARE_VALIDATION,
    tested_duration_s=None,
    max_duration_s=30.0,
    enforce_max_duration=False,
    expected_bitrate_mbps=3600.0,
    description="Legacy RAM-spooled AVI lossless calibration profile.",
    recommended_use="Compatibility only; revalidate and verify real-session source hashes.",
    writer_defaults={
        **_lossless_writer_defaults(3600.0, segment_seconds=30, gop_frames=1200),
        "mode": "lossless_nvenc_spooled",
        "container": "avi",
        "spool_output": True,
        "spool_chunk_bytes": 8 * 1024 * 1024,
        "spool_max_bytes": 32 * 1024**3,
        "finalize_timeout_s": 1800,
    },
)


USB_MONO8_LOSSLESS_H264_NVENC_MP4 = RecordingProfileDefinition(
    id="usb_mono8_lossless_h264_nvenc_mp4",
    display_name="USB Mono8 lossless H.264 NVENC MP4",
    version=PROFILE_VERSION,
    compression="lossless_h264_nvenc_mp4",
    pixel_fidelity="lossless",
    validation_status=REQUIRES_HARDWARE_VALIDATION,
    tested_duration_s=None,
    max_duration_s=None,
    enforce_max_duration=False,
    expected_bitrate_mbps=900.0,
    description="Lossless MP4 sized for the acA1300 USB Mono8 path.",
    recommended_use="USB Mono8 runs requiring exact pixels after real-session hash verification.",
    writer_defaults=_lossless_writer_defaults(900.0, segment_seconds=120, gop_frames=480),
)


CXP_MONO8_LOSSLESS_H264_NVENC_MP4 = RecordingProfileDefinition(
    id="cxp_mono8_lossless_h264_nvenc_mp4",
    display_name="CXP Mono8 lossless H.264 NVENC MP4",
    version=PROFILE_VERSION,
    compression="lossless_h264_nvenc_mp4",
    pixel_fidelity="lossless",
    validation_status=REQUIRES_HARDWARE_VALIDATION,
    tested_duration_s=None,
    max_duration_s=None,
    enforce_max_duration=False,
    expected_bitrate_mbps=4000.0,
    description=(
        "Lossless MP4 sized for a2A2448 CXP evidence of roughly 3.7-4.0 Gbps. "
        "The current 60-second evidence lacks queue margin, so this profile is not locked."
    ),
    recommended_use="Engineering validation only until rollover, queue margin, and intended preview mode pass.",
    writer_defaults=_lossless_writer_defaults(4000.0, segment_seconds=30, gop_frames=1200),
)


USB_BAYER8_LOSSLESS_H264_NVENC_GPU = RecordingProfileDefinition(
    id="usb_bayer8_lossless_h264_nvenc_gpu",
    display_name="USB Bayer8 lossless H.264 NVENC MP4",
    version=PROFILE_VERSION,
    compression="lossless_h264_nvenc",
    pixel_fidelity="lossless_bayer_luma",
    validation_status=REQUIRES_HARDWARE_VALIDATION,
    tested_duration_s=None,
    max_duration_s=None,
    enforce_max_duration=False,
    expected_bitrate_mbps=1200.0,
    description="Stores the one-byte raw Bayer mosaic through the lossless luma path.",
    recommended_use="Raw-color calibration after exact real-session Bayer-byte verification.",
    writer_defaults=_lossless_writer_defaults(1200.0, segment_seconds=120, gop_frames=480),
)


ANALYSIS_H264_MP4_100M = RecordingProfileDefinition(
    id="analysis_h264_mp4_100m",
    display_name="CV-optimal H.264 NVENC 100 Mbps MP4",
    version=PROFILE_VERSION,
    compression="analysis_h264_nvenc_mp4",
    pixel_fidelity="lossy_analysis",
    validation_status=REQUIRES_HARDWARE_VALIDATION,
    tested_duration_s=None,
    max_duration_s=None,
    enforce_max_duration=False,
    expected_bitrate_mbps=100.0,
    description="Small direct MP4 for the full-frame acA1300 Mono8 data rate.",
    recommended_use="Default USB computer-vision recording when exact intensities are not required.",
    writer_defaults=_cbr_writer_defaults(100.0, gop_frames=480),
)


ANALYSIS_H264_MP4_250M = RecordingProfileDefinition(
    id="analysis_h264_mp4_250m",
    display_name="CV-optimal H.264 NVENC 250 Mbps MP4",
    version=PROFILE_VERSION,
    compression="analysis_h264_nvenc_mp4",
    pixel_fidelity="lossy_analysis",
    validation_status=REQUIRES_HARDWARE_VALIDATION,
    tested_duration_s=None,
    max_duration_s=None,
    enforce_max_duration=False,
    expected_bitrate_mbps=250.0,
    description="Small direct MP4 for full-frame 200 fps CXP Mono8 acquisition.",
    recommended_use="Default CXP computer-vision recording with preview off until preview-on evidence passes.",
    writer_defaults=_cbr_writer_defaults(250.0, gop_frames=1200),
)


ANALYSIS_H264_MP4_27M = RecordingProfileDefinition(
    id="analysis_h264_mp4_27m",
    display_name="Compact CXP H.264 NVENC 27 Mbps MP4",
    version=PROFILE_VERSION,
    compression="analysis_h264_nvenc_mp4",
    pixel_fidelity="high_compression_lossy_analysis",
    validation_status=REQUIRES_HARDWARE_VALIDATION,
    tested_duration_s=None,
    max_duration_s=None,
    enforce_max_duration=False,
    expected_bitrate_mbps=27.0,
    description=(
        "Full-frame 2464x2064 at 200 fps with a 27 Mbps CBR target, approximately "
        "202.5 MB per 60 seconds before small container overhead."
    ),
    recommended_use=(
        "Storage-constrained computer-vision experiments after task-specific quality and "
        "real-time hardware validation; compression is intentionally aggressive."
    ),
    writer_defaults=_cbr_writer_defaults(27.0, gop_frames=1200),
)


ANALYSIS_H264_MP4_RGB_200M = RecordingProfileDefinition(
    id="analysis_h264_mp4_rgb_200m",
    display_name="RGB source H.264 NVENC 200 Mbps MP4",
    version=PROFILE_VERSION,
    compression="analysis_h264_nvenc_mp4",
    pixel_fidelity="lossy_color_analysis",
    validation_status=REQUIRES_HARDWARE_VALIDATION,
    tested_duration_s=None,
    max_duration_s=None,
    enforce_max_duration=False,
    expected_bitrate_mbps=200.0,
    description="Direct RGB8/BGR8 to yuv420p MP4; color conversion is intentionally lossy.",
    recommended_use="Engineering color convenience only; raw Bayer is safer when exact source bytes matter.",
    writer_defaults={
        **_cbr_writer_defaults(200.0, gop_frames=480),
        "input_pix_fmt": "rgb24",
    },
)


PROFILE_LIBRARY: dict[str, RecordingProfileDefinition] = {
    profile.id: profile
    for profile in (
        LONG_LOSSY_H264_250M,
        NEAR_LOSSLESS_H264_400M,
        LOSSLESS_H264_NVENC_GPU,
        USB_BAYER8_LOSSLESS_H264_NVENC_GPU,
        USB_MONO8_LOSSLESS_H264_NVENC_MP4,
        CXP_MONO8_LOSSLESS_H264_NVENC_MP4,
        ANALYSIS_H264_MP4_100M,
        ANALYSIS_H264_MP4_250M,
        ANALYSIS_H264_MP4_27M,
        ANALYSIS_H264_MP4_RGB_200M,
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


def profile_claims_losslessness(pixel_fidelity: str) -> bool:
    """Return whether real-session source-vs-decode evidence is mandatory."""

    return str(pixel_fidelity).strip().lower().startswith("lossless")
