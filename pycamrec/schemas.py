"""Typed runtime schemas for PyCamRec."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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
    temperature_critical_c: float = 76.0
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
    schema_version: int = 2
    project_id: str = "UNSPECIFIED"
    protocol_id: str = "UNSPECIFIED"
    assay_id: str = "UNSPECIFIED"
    subject_id: str = "UNSPECIFIED"
    species: str = "UNSPECIFIED"
    date_of_birth: str = ""
    postnatal_day: int | None = None
    postnatal_day_source: str = "manual"
    p0_convention: str = "birth_date_is_p0"
    weight_g: float | None = None
    weight_measured_utc: str = ""
    genotype: str = "UNSPECIFIED"
    experimental_group: str = "UNSPECIFIED"
    sex: str = "UNSPECIFIED"
    experimenter_id: str = "UNSPECIFIED"
    run_index: int = 1
    custom_fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: str = ""

    def missing_fields(self) -> list[str]:
        missing = []
        for field_name in (
            "project_id",
            "protocol_id",
            "assay_id",
            "subject_id",
            "species",
            "postnatal_day",
            "weight_g",
            "genotype",
            "experimental_group",
            "sex",
            "experimenter_id",
        ):
            value = getattr(self, field_name)
            if value is None or (isinstance(value, str) and value.strip().upper() in {"", "UNSPECIFIED"}):
                missing.append(field_name)
        return missing

    def validation_issues(self, *, recording_date: date | None = None) -> list[str]:
        """Return semantic metadata errors independently from missing fields."""

        issues: list[str] = []
        if self.schema_version != 2:
            issues.append(f"schema_version must be 2, got {self.schema_version!r}")
        if self.postnatal_day is not None and (
            isinstance(self.postnatal_day, bool) or self.postnatal_day < 0
        ):
            issues.append("postnatal_day must be a non-negative integer")
        if self.weight_g is not None and (
            not math.isfinite(self.weight_g) or self.weight_g <= 0
        ):
            issues.append("weight_g must be a positive finite number")
        if self.run_index <= 0:
            issues.append("run_index must be a positive integer")
        if self.p0_convention != "birth_date_is_p0":
            issues.append("p0_convention must be 'birth_date_is_p0'")
        if self.postnatal_day_source not in {"manual", "derived_from_date_of_birth"}:
            issues.append(
                "postnatal_day_source must be 'manual' or 'derived_from_date_of_birth'"
            )

        allowed_sex = {"female", "male", "intersex", "unknown", "not_applicable"}
        sex = self.sex.strip().lower()
        if sex not in {"", "unspecified"} and sex not in allowed_sex:
            issues.append("sex must be female, male, intersex, unknown, or not_applicable")

        birth_date: date | None = None
        if self.date_of_birth.strip():
            try:
                birth_date = date.fromisoformat(self.date_of_birth.strip())
            except ValueError:
                issues.append("date_of_birth must use ISO YYYY-MM-DD format")
        if birth_date is not None and self.postnatal_day is not None:
            reference_date = recording_date or datetime.now(timezone.utc).date()
            expected_pnd = (reference_date - birth_date).days
            if expected_pnd < 0:
                issues.append("date_of_birth cannot be after the recording date")
            elif expected_pnd != self.postnatal_day:
                issues.append(
                    f"postnatal_day {self.postnatal_day} does not match date_of_birth "
                    f"for {reference_date.isoformat()} (expected {expected_pnd})"
                )
        if self.postnatal_day_source == "derived_from_date_of_birth" and birth_date is None:
            issues.append("date_of_birth is required when postnatal_day_source is derived")

        if self.weight_measured_utc.strip():
            try:
                measured = datetime.fromisoformat(self.weight_measured_utc.replace("Z", "+00:00"))
            except ValueError:
                issues.append("weight_measured_utc must be an ISO-8601 timestamp")
            else:
                if measured.tzinfo is None:
                    issues.append("weight_measured_utc must include a timezone")

        for key, item in self.custom_fields.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
                issues.append(
                    f"custom field {key!r} must be lower-case snake_case and at most 64 characters"
                )
                continue
            if not isinstance(item, dict):
                issues.append(f"custom field {key!r} must be a mapping")
                continue
            value_type = str(item.get("value_type") or "").strip().lower()
            if value_type not in {"string", "number", "integer", "boolean"}:
                issues.append(
                    f"custom field {key!r} value_type must be string, number, integer, or boolean"
                )
            if "value" not in item or item.get("value") is None or item.get("value") == "":
                issues.append(f"custom field {key!r} requires a value")
                continue
            custom_value = item.get("value")
            type_matches = {
                "string": isinstance(custom_value, str),
                "number": isinstance(custom_value, (int, float)) and not isinstance(custom_value, bool),
                "integer": isinstance(custom_value, int) and not isinstance(custom_value, bool),
                "boolean": isinstance(custom_value, bool),
            }.get(value_type, True)
            if not type_matches:
                issues.append(
                    f"custom field {key!r} value does not match declared {value_type!r} type"
                )
        return issues

    def readiness_issues(self, *, recording_date: date | None = None) -> list[str]:
        return [f"missing:{name}" for name in self.missing_fields()] + self.validation_issues(
            recording_date=recording_date
        )


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
