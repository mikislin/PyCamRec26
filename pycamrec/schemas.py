"""Typed runtime schemas for PyCamRec."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .pixel_formats import bytes_per_pixel


@dataclass(frozen=True)
class SessionConfig:
    output_root: Path
    duration_s: float
    name: str = "session"
    operator: str = ""
    notes: str = ""


@dataclass(frozen=True)
class CameraConfig:
    make: str
    serial: str
    pfs_path: Path
    expected_width: int
    expected_height: int
    expected_pixel_format: str
    expected_fps: float
    grab_timeout_ms: int = 2000
    max_num_buffer: int = 512
    enable_chunks: bool = True
    strict_validation: bool = True
    allow_runtime_pixel_format_override: bool = False
    allow_runtime_frame_rate_override: bool = False
    temperature_warning_c: float = 40.0
    health_check_interval_s: float = 10.0


@dataclass(frozen=True)
class WriterConfig:
    mode: str = "compressed_nvenc"
    segment_seconds: float = 120.0
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    input_pix_fmt: str = "gray"
    codec: str = "hevc_nvenc"
    container: str = "avi"
    output_pix_fmt: str = "yuv420p"
    queue_max_frames: int = 256
    output_args: tuple[str, ...] = field(default_factory=tuple)
    overwrite: bool = False
    hash_segments: bool = False
    expected_bitrate_mbps: float | None = None
    min_free_space_gb: float = 10.0
    spool_output: bool = False
    spool_chunk_bytes: int = 8 * 1024 * 1024
    spool_max_bytes: int = 32 * 1024**3
    finalize_timeout_s: float = 60.0


@dataclass(frozen=True)
class RecordingProfileConfig:
    id: str = "custom"
    display_name: str = "Custom writer settings"
    version: str = ""
    compression: str = "custom"
    pixel_fidelity: str = "custom"
    validation_status: str = "untested"
    tested_duration_s: float | None = None
    max_duration_s: float | None = None
    enforce_max_duration: bool = False
    expected_bitrate_mbps: float | None = None
    description: str = ""
    recommended_use: str = ""


@dataclass(frozen=True)
class ExperimentMetadataConfig:
    animal_id: str = "UNSPECIFIED"
    dob: str = "UNSPECIFIED"
    test_assay_name: str = "UNSPECIFIED"
    genotype: str = "UNSPECIFIED"
    experimental_group: str = "UNSPECIFIED"
    sex: str = "UNSPECIFIED"
    experimentator: str = "UNSPECIFIED"
    project_protocol: str = "UNSPECIFIED"
    camera_profile_path: str = "UNSPECIFIED"
    notes: str = ""

    def missing_fields(self) -> list[str]:
        missing = []
        for field_name in (
            "animal_id",
            "dob",
            "test_assay_name",
            "genotype",
            "experimental_group",
            "sex",
            "experimentator",
            "project_protocol",
            "camera_profile_path",
        ):
            value = getattr(self, field_name)
            if value.strip() in {"", "UNSPECIFIED"}:
                missing.append(field_name)
        return missing


@dataclass(frozen=True)
class MetadataConfig:
    per_frame_format: str = "csv"
    flush_every_frames: int = 200
    record_host_timestamps: bool = True
    source_frame_hash_every: int = 0
    source_frame_hash_max_frames: int = 0


@dataclass(frozen=True)
class PreviewConfig:
    enabled: bool = False
    max_fps: float = 10.0
    width: int = 512
    height: int | None = None
    sample_every: int | None = None
    overlay: bool = True
    window_title: str = "PyCamRec Preview"
    sink: str = "window"
    image_path: Path | None = None
    shed_queue_fraction: float = 0.10
    shed_fps_ratio: float = 0.99
    throttle_cooldown_s: float = 2.0
    opencv_threads: int = 1


@dataclass(frozen=True)
class PyCamRecConfig:
    session: SessionConfig
    camera: CameraConfig
    writer: WriterConfig = field(default_factory=WriterConfig)
    recording_profile: RecordingProfileConfig = field(default_factory=RecordingProfileConfig)
    experiment: ExperimentMetadataConfig = field(default_factory=ExperimentMetadataConfig)
    metadata: MetadataConfig = field(default_factory=MetadataConfig)
    preview: PreviewConfig = field(default_factory=PreviewConfig)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def frame_bytes(self) -> int:
        return (
            self.camera.expected_width
            * self.camera.expected_height
            * bytes_per_pixel(self.camera.expected_pixel_format)
        )

    @property
    def expected_total_frames(self) -> int:
        return int(round(self.session.duration_s * self.camera.expected_fps))

    @property
    def segment_frame_count(self) -> int:
        return max(1, int(round(self.writer.segment_seconds * self.camera.expected_fps)))
