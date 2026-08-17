"""Configuration loading and validation."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .camera_backend import normalize_camera_make, supported_camera_makes
from .pixel_formats import ffmpeg_raw_pix_fmt
from .profiles import get_profile, profile_config_dict
from .schemas import (
    CameraConfig,
    ExperimentMetadataConfig,
    MetadataConfig,
    PreviewConfig,
    PyCamRecConfig,
    RecordingProfileConfig,
    SessionConfig,
    WriterConfig,
)


def load_config(
    path: str | os.PathLike[str],
    duration_s: float | None = None,
    output_root: str | os.PathLike[str] | None = None,
    preview_enabled: bool | None = None,
    preview_width: int | None = None,
    preview_max_fps: float | None = None,
) -> PyCamRecConfig:
    config_path = Path(path).expanduser().resolve()
    data = _load_mapping(config_path)
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a mapping: {config_path}")

    if duration_s is not None:
        if duration_s <= 0:
            raise ValueError("duration override must be positive.")
        data = dict(data)
        session_override = dict(data.get("session", {}))
        session_override["duration_s"] = duration_s
        data["session"] = session_override
        runtime_overrides = dict(data.get("runtime_overrides", {}))
        runtime_overrides["duration_s"] = duration_s
        data["runtime_overrides"] = runtime_overrides

    if output_root is not None:
        data = dict(data)
        session_override = dict(data.get("session", {}))
        session_override["output_root"] = str(output_root)
        data["session"] = session_override
        runtime_overrides = dict(data.get("runtime_overrides", {}))
        runtime_overrides["output_root"] = str(output_root)
        data["runtime_overrides"] = runtime_overrides

    if preview_enabled is not None or preview_width is not None or preview_max_fps is not None:
        data = dict(data)
        preview_override = dict(data.get("preview", {}))
        if preview_enabled is not None:
            preview_override["enabled"] = preview_enabled
        if preview_width is not None:
            preview_override["width"] = preview_width
        if preview_max_fps is not None:
            preview_override["max_fps"] = preview_max_fps
        data["preview"] = preview_override

    base_dir = config_path.parent
    session_data = dict(data.get("session", {}))
    camera_data = dict(data.get("camera", {}))
    profile_data = data.get("profile") or data.get("recording_profile")
    profile_config, profile_writer_defaults = _load_profile(profile_data)
    writer_data = _merge_dicts(profile_writer_defaults, dict(data.get("writer", {})))
    experiment_data = dict(data.get("experiment", {}))
    project_data = dict(experiment_data.get("project", {}))
    subject_data = dict(experiment_data.get("subject", {}))
    acquisition_data = dict(experiment_data.get("acquisition", {}))
    metadata_data = dict(data.get("metadata", {}))
    preview_data = dict(data.get("preview", {}))

    duration_value = session_data.get("duration_s")
    if duration_value is None:
        raise ValueError(
            "session.duration_s is not set; provide a duration on the command line, "
            "for example --duration-s 60 or --60."
        )

    session = SessionConfig(
        output_root=_resolve_path(session_data["output_root"], base_dir),
        duration_s=float(duration_value),
        name=str(session_data.get("name", "session")),
        operator=str(session_data.get("operator", "")),
        notes=str(session_data.get("notes", "")),
    )

    camera = CameraConfig(
        make=normalize_camera_make(str(camera_data.get("make", "basler"))),
        serial=str(camera_data["serial"]),
        pfs_path=_resolve_path(camera_data["pfs_path"], base_dir),
        expected_width=int(camera_data["expected_width"]),
        expected_height=int(camera_data["expected_height"]),
        expected_pixel_format=str(camera_data["expected_pixel_format"]),
        expected_fps=float(camera_data["expected_fps"]),
        grab_timeout_ms=int(camera_data.get("grab_timeout_ms", 2000)),
        max_num_buffer=int(camera_data.get("max_num_buffer", 512)),
        enable_chunks=bool(camera_data.get("enable_chunks", True)),
        strict_validation=bool(camera_data.get("strict_validation", True)),
        allow_runtime_pixel_format_override=bool(camera_data.get("allow_runtime_pixel_format_override", False)),
        allow_runtime_frame_rate_override=bool(camera_data.get("allow_runtime_frame_rate_override", False)),
        temperature_warning_c=float(camera_data.get("temperature_warning_c", 40.0)),
        temperature_critical_c=float(camera_data.get("temperature_critical_c", 76.0)),
        health_check_interval_s=float(camera_data.get("health_check_interval_s", 10.0)),
    )

    output_args = writer_data.get("output_args", ())
    if output_args is None:
        output_args = ()
    if not isinstance(output_args, (list, tuple)):
        raise ValueError("writer.output_args must be a list of ffmpeg arguments.")

    writer = WriterConfig(
        mode=str(writer_data.get("mode", "compressed_nvenc")),
        segment_seconds=float(writer_data.get("segment_seconds", 120.0)),
        ffmpeg_path=str(writer_data.get("ffmpeg_path", "ffmpeg")),
        ffprobe_path=str(writer_data.get("ffprobe_path", "ffprobe")),
        input_pix_fmt=str(writer_data.get("input_pix_fmt", "gray")),
        codec=str(writer_data.get("codec", "hevc_nvenc")),
        container=str(writer_data.get("container", "avi")).lstrip("."),
        output_pix_fmt=str(writer_data.get("output_pix_fmt", "yuv420p")),
        queue_max_frames=int(writer_data.get("queue_max_frames", 256)),
        output_args=tuple(str(arg) for arg in output_args),
        overwrite=bool(writer_data.get("overwrite", False)),
        hash_segments=bool(writer_data.get("hash_segments", False)),
        expected_bitrate_mbps=(
            float(writer_data["expected_bitrate_mbps"])
            if writer_data.get("expected_bitrate_mbps") is not None
            else None
        ),
        min_free_space_gb=float(writer_data.get("min_free_space_gb", 10.0)),
        spool_output=bool(writer_data.get("spool_output", False)),
        spool_chunk_bytes=int(writer_data.get("spool_chunk_bytes", 8 * 1024 * 1024)),
        spool_max_bytes=int(writer_data.get("spool_max_bytes", 32 * 1024**3)),
        finalize_timeout_s=float(writer_data.get("finalize_timeout_s", 60.0)),
    )

    metadata = MetadataConfig(
        per_frame_format=str(metadata_data.get("per_frame_format", "csv")),
        flush_every_frames=int(metadata_data.get("flush_every_frames", 200)),
        record_host_timestamps=bool(metadata_data.get("record_host_timestamps", True)),
        source_frame_hash_every=int(metadata_data.get("source_frame_hash_every", 0)),
        source_frame_hash_max_frames=int(metadata_data.get("source_frame_hash_max_frames", 0)),
    )

    experiment = ExperimentMetadataConfig(
        schema_version=int(experiment_data.get("schema_version", 2)),
        project_id=str(
            project_data.get("project_id", experiment_data.get("project_id", "UNSPECIFIED"))
        ),
        protocol_id=str(
            project_data.get(
                "protocol_id",
                experiment_data.get("protocol_id", experiment_data.get("project_protocol", "UNSPECIFIED")),
            )
        ),
        assay_id=str(
            project_data.get(
                "assay_id",
                experiment_data.get("assay_id", experiment_data.get("test_assay_name", "UNSPECIFIED")),
            )
        ),
        subject_id=str(
            subject_data.get(
                "subject_id",
                experiment_data.get("subject_id", experiment_data.get("animal_id", "UNSPECIFIED")),
            )
        ),
        species=str(subject_data.get("species", experiment_data.get("species", "UNSPECIFIED"))),
        date_of_birth=_optional_metadata_string(
            subject_data.get(
                "date_of_birth",
                experiment_data.get("date_of_birth", experiment_data.get("dob", "")),
            )
        ),
        postnatal_day=_optional_int(
            subject_data.get("postnatal_day", experiment_data.get("postnatal_day")),
            "experiment.subject.postnatal_day",
        ),
        postnatal_day_source=str(
            subject_data.get(
                "postnatal_day_source",
                experiment_data.get("postnatal_day_source", "manual"),
            )
        ),
        p0_convention=str(
            subject_data.get(
                "p0_convention",
                experiment_data.get("p0_convention", "birth_date_is_p0"),
            )
        ),
        weight_g=_optional_float(
            subject_data.get("weight_g", experiment_data.get("weight_g")),
            "experiment.subject.weight_g",
        ),
        weight_measured_utc=str(
            subject_data.get(
                "weight_measured_utc",
                experiment_data.get("weight_measured_utc", ""),
            )
        ),
        genotype=str(subject_data.get("genotype", experiment_data.get("genotype", "UNSPECIFIED"))),
        experimental_group=str(
            subject_data.get(
                "experimental_group",
                experiment_data.get("experimental_group", "UNSPECIFIED"),
            )
        ),
        sex=str(subject_data.get("sex", experiment_data.get("sex", "UNSPECIFIED"))),
        experimenter_id=str(
            acquisition_data.get(
                "experimenter_id",
                experiment_data.get("experimenter_id", experiment_data.get("experimentator", "UNSPECIFIED")),
            )
        ),
        run_index=int(acquisition_data.get("run_index", experiment_data.get("run_index", 1))),
        custom_fields=_normalize_custom_fields(experiment_data.get("custom_fields", {})),
        notes=str(experiment_data.get("notes", "")),
    )

    preview_height = preview_data.get("height")
    preview_sample_every = preview_data.get("sample_every")
    preview = PreviewConfig(
        enabled=bool(preview_data.get("enabled", False)),
        max_fps=float(preview_data.get("max_fps", 10.0)),
        width=int(preview_data.get("width", 512)),
        height=None if preview_height in (None, "") else int(preview_height),
        sample_every=None if preview_sample_every in (None, "") else int(preview_sample_every),
        overlay=bool(preview_data.get("overlay", True)),
        window_title=str(preview_data.get("window_title", "PyCamRec Preview")),
        sink=str(preview_data.get("sink", "window")),
        image_path=(
            None
            if preview_data.get("image_path") in (None, "")
            else _resolve_path(preview_data["image_path"], base_dir)
        ),
        shed_queue_fraction=float(preview_data.get("shed_queue_fraction", 0.10)),
        shed_fps_ratio=float(preview_data.get("shed_fps_ratio", 0.99)),
        throttle_cooldown_s=float(preview_data.get("throttle_cooldown_s", 2.0)),
        opencv_threads=int(preview_data.get("opencv_threads", 1)),
    )

    cfg = PyCamRecConfig(
        session=session,
        camera=camera,
        writer=writer,
        recording_profile=profile_config,
        experiment=experiment,
        metadata=metadata,
        preview=preview,
        raw=data,
    )
    validate_config(cfg)
    return cfg


def validate_config(cfg: PyCamRecConfig) -> None:
    camera_make = normalize_camera_make(cfg.camera.make)
    if camera_make not in supported_camera_makes():
        choices = ", ".join(supported_camera_makes())
        raise ValueError(f"Unsupported camera.make {cfg.camera.make!r}. Supported values: {choices}.")
    if not cfg.camera.serial:
        raise ValueError("camera.serial is required.")
    if not cfg.camera.pfs_path.is_file():
        raise FileNotFoundError(f"Camera .pfs file not found: {cfg.camera.pfs_path}")
    if cfg.camera.strict_validation:
        pfs_serial = _serial_token_from_pfs_name(cfg.camera.pfs_path)
        if pfs_serial and pfs_serial != cfg.camera.serial:
            raise ValueError(
                f"Camera .pfs filename appears to belong to serial {pfs_serial!r}, "
                f"but config camera.serial is {cfg.camera.serial!r}: {cfg.camera.pfs_path}"
            )
    if cfg.camera.expected_width <= 0 or cfg.camera.expected_height <= 0:
        raise ValueError("Expected camera width and height must be positive.")
    if cfg.camera.expected_fps <= 0:
        raise ValueError("Expected FPS must be positive.")
    expected_input_pix_fmt = ffmpeg_raw_pix_fmt(cfg.camera.expected_pixel_format)
    if cfg.writer.input_pix_fmt != expected_input_pix_fmt:
        raise ValueError(
            "writer.input_pix_fmt does not match camera.expected_pixel_format: "
            f"expected {expected_input_pix_fmt!r} for {cfg.camera.expected_pixel_format!r}, "
            f"got {cfg.writer.input_pix_fmt!r}."
        )
    if cfg.camera.grab_timeout_ms <= 0:
        raise ValueError("camera.grab_timeout_ms must be positive.")
    if cfg.camera.max_num_buffer <= 0:
        raise ValueError("camera.max_num_buffer must be positive.")
    if cfg.camera.temperature_warning_c <= 0:
        raise ValueError("camera.temperature_warning_c must be positive.")
    if cfg.camera.temperature_critical_c <= cfg.camera.temperature_warning_c:
        raise ValueError("camera.temperature_critical_c must be greater than temperature_warning_c.")
    if cfg.camera.health_check_interval_s <= 0:
        raise ValueError("camera.health_check_interval_s must be positive.")
    if cfg.session.duration_s <= 0:
        raise ValueError("session.duration_s must be positive.")
    if (
        cfg.recording_profile.max_duration_s is not None
        and cfg.session.duration_s > cfg.recording_profile.max_duration_s
        and cfg.recording_profile.enforce_max_duration
    ):
        raise ValueError(
            f"profile {cfg.recording_profile.id!r} is limited to "
            f"{cfg.recording_profile.max_duration_s:g} seconds; "
            f"config requests {cfg.session.duration_s:g} seconds."
        )
    if cfg.writer.segment_seconds <= 0:
        raise ValueError("writer.segment_seconds must be positive.")
    if cfg.writer.queue_max_frames <= 0:
        raise ValueError("writer.queue_max_frames must be positive.")
    if cfg.writer.min_free_space_gb < 0:
        raise ValueError("writer.min_free_space_gb cannot be negative.")
    if cfg.writer.spool_chunk_bytes <= 0:
        raise ValueError("writer.spool_chunk_bytes must be positive.")
    if cfg.writer.spool_max_bytes < cfg.writer.spool_chunk_bytes:
        raise ValueError("writer.spool_max_bytes must be at least writer.spool_chunk_bytes.")
    if cfg.writer.finalize_timeout_s <= 0:
        raise ValueError("writer.finalize_timeout_s must be positive.")
    if cfg.recording_profile.id != "custom" and cfg.writer.container.lower() not in {"avi", "mp4", "mkv"}:
        raise ValueError("Built-in recording profiles must write AVI, MP4, or MKV segment files.")
    if cfg.metadata.per_frame_format.lower() != "csv":
        raise ValueError("Phase 1 implements metadata.per_frame_format: csv.")
    if cfg.metadata.flush_every_frames <= 0:
        raise ValueError("metadata.flush_every_frames must be positive.")
    if cfg.metadata.source_frame_hash_every < 0:
        raise ValueError("metadata.source_frame_hash_every cannot be negative.")
    if cfg.metadata.source_frame_hash_max_frames < 0:
        raise ValueError("metadata.source_frame_hash_max_frames cannot be negative.")
    if cfg.metadata.source_frame_hash_max_frames > 0 and cfg.metadata.source_frame_hash_every <= 0:
        raise ValueError("metadata.source_frame_hash_every must be positive when a hash maximum is set.")
    if cfg.preview.max_fps <= 0:
        raise ValueError("preview.max_fps must be positive.")
    if cfg.preview.width <= 0:
        raise ValueError("preview.width must be positive.")
    if cfg.preview.height is not None and cfg.preview.height <= 0:
        raise ValueError("preview.height must be positive when set.")
    if cfg.preview.sample_every is not None and cfg.preview.sample_every <= 0:
        raise ValueError("preview.sample_every must be positive when set.")
    if not 0 < cfg.preview.shed_queue_fraction < 1:
        raise ValueError("preview.shed_queue_fraction must be between 0 and 1.")
    if not 0 < cfg.preview.shed_fps_ratio <= 1:
        raise ValueError("preview.shed_fps_ratio must be between 0 and 1.")
    if cfg.preview.throttle_cooldown_s <= 0:
        raise ValueError("preview.throttle_cooldown_s must be positive.")
    if cfg.preview.opencv_threads <= 0:
        raise ValueError("preview.opencv_threads must be positive.")
    if cfg.preview.sink not in {"window", "file", "pgm", "shm", "shm_raw"}:
        raise ValueError("preview.sink must be 'window', 'file', 'pgm', 'shm', or 'shm_raw'.")
    if cfg.preview.sink in {"file", "pgm", "shm", "shm_raw"} and cfg.preview.enabled and cfg.preview.image_path is None:
        raise ValueError("preview.image_path is required for file-backed preview status.")
    metadata_issues = cfg.experiment.validation_issues()
    if metadata_issues:
        raise ValueError("Invalid experiment metadata: " + "; ".join(metadata_issues))


def _load_mapping(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)

    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("Install PyYAML to load YAML config files.") from exc
    return yaml.safe_load(text)


def _load_profile(profile_data: Any) -> tuple[RecordingProfileConfig, dict[str, Any]]:
    if profile_data in (None, "", False):
        return RecordingProfileConfig(), {}
    if isinstance(profile_data, str):
        profile_id = profile_data
        enforce_max_duration = None
    elif isinstance(profile_data, dict):
        profile_id = str(profile_data["id"])
        enforce_max_duration = (
            bool(profile_data["enforce_max_duration"])
            if profile_data.get("enforce_max_duration") is not None
            else None
        )
    else:
        raise ValueError("profile must be a string profile id or a mapping with an id field.")

    definition = get_profile(profile_id)
    config_data = profile_config_dict(definition, enforce_max_duration=enforce_max_duration)
    config = RecordingProfileConfig(**config_data)
    return config, dict(definition.writer_defaults)


def _serial_token_from_pfs_name(path: Path) -> str:
    match = re.search(r"\d{6,}", path.name)
    return match.group(0) if match else ""


def _merge_dicts(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = dict(defaults)
    merged.update(overrides)
    return merged


def _resolve_path(value: str | os.PathLike[str], base_dir: Path) -> Path:
    raw = os.path.expandvars(os.path.expanduser(str(value)))
    path = Path(raw)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _optional_int(value: Any, field_name: str) -> int | None:
    if value in (None, "", "UNSPECIFIED"):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer.")
    try:
        converted = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc
    if isinstance(value, float) and value != converted:
        raise ValueError(f"{field_name} must be an integer.")
    return converted


def _optional_metadata_string(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.upper() == "UNSPECIFIED" else text


def _optional_float(value: Any, field_name: str) -> float | None:
    if value in (None, "", "UNSPECIFIED"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric.") from exc


def _normalize_custom_fields(value: Any) -> dict[str, dict[str, Any]]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("experiment.custom_fields must be a mapping.")
    result: dict[str, dict[str, Any]] = {}
    for key, raw_item in value.items():
        name = str(key).strip()
        if isinstance(raw_item, dict):
            item = dict(raw_item)
        else:
            item = {"value": raw_item, "value_type": _custom_value_type(raw_item)}
        item.setdefault("value_type", _custom_value_type(item.get("value")))
        if item.get("unit") is None:
            item.pop("unit", None)
        result[name] = item
    return result


def _custom_value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"
