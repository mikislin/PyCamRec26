"""Command line interface."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any

from .analysis_export import export_session_for_analysis
from .analysis_io import inspect_video_read
from .approval import finalize_qualification_from_summary, lock_profile_from_summary
from .acquisition import Recorder
from .config import load_config
from .diagnostics import inspect_camera_capabilities, inspect_pylon
from .onboarding import generate_camera_config
from .preflight import run_preflight
from .preview_session import PreviewSession, stats_to_dict
from .profiles import list_profile_dicts
from .qualification import build_qualification_plan
from .report import build_session_report
from .session_index import write_session_index
from .verify import benchmark_codecs, verify_lossless_codec, verify_session_pixels


def main(argv: list[str] | None = None) -> int:
    argv = _normalize_duration_shorthand(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="pycamrec")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("devices", help="Show what pypylon can enumerate.")
    subparsers.add_parser("gui", help="Launch the PyCamRec Windows desktop app.")
    subparsers.add_parser("profiles", help="List built-in recording profiles.")

    capabilities_parser = subparsers.add_parser(
        "camera-capabilities",
        help="Open a Basler camera briefly and report pixel formats and frame-rate limits.",
    )
    capabilities_parser.add_argument("--serial")

    report_parser = subparsers.add_parser("report", help="Summarize a completed PyCamRec session.")
    report_parser.add_argument("session_dir", type=Path)
    report_parser.add_argument(
        "--output",
        type=Path,
        help="Also write the JSON report to this path (refuses to overwrite an existing file).",
    )

    index_parser = subparsers.add_parser(
        "index",
        help="Build a portable one-row-per-session CSV or JSONL index for analysis.",
    )
    index_parser.add_argument("output_root", type=Path)
    index_parser.add_argument("--output", type=Path, required=True)
    index_parser.add_argument("--format", choices=("csv", "jsonl"), default="csv")
    index_parser.add_argument(
        "--include-qc",
        action="store_true",
        help="Run report/FFprobe for each session and include current readiness columns.",
    )

    qualification_parser = subparsers.add_parser(
        "qualification-plan",
        help="Show the cases, commands, and storage budget for CXP profile qualification without recording.",
    )
    qualification_parser.add_argument("config", type=Path)

    read_video_parser = subparsers.add_parser(
        "read-video",
        help="Inspect how OpenCV will expose a PyCamRec segment for analysis code.",
    )
    read_video_parser.add_argument("video", type=Path)
    read_video_parser.add_argument("--mode", choices=("auto", "gray", "color", "raw"), default="auto")
    read_video_parser.add_argument("--frame", type=int, default=0)

    export_parser = subparsers.add_parser(
        "export-analysis",
        help="Export a completed session to CV-friendly MP4 files without changing the original recording.",
    )
    export_parser.add_argument("session_dir", type=Path)
    export_parser.add_argument("--output-dir", type=Path)
    export_parser.add_argument(
        "--mode",
        choices=("auto", "debayer", "gray", "passthrough"),
        default="auto",
        help="auto debayers Bayer sessions and writes gray-compatible MP4 for mono sessions.",
    )
    export_parser.add_argument("--encoder", choices=("libx264", "h264_nvenc"), default="libx264")
    export_parser.add_argument("--crf", type=int, default=16, help="libx264 quality value; lower is higher quality.")
    export_parser.add_argument("--qp", type=int, default=16, help="h264_nvenc constant-QP value; lower is higher quality.")
    export_parser.add_argument("--max-frames", type=int, help="Debug/test export limit.")

    codec_parser = subparsers.add_parser(
        "verify-codec",
        help="Verify that a profile's encoder settings round-trip synthetic gray frames losslessly.",
    )
    codec_parser.add_argument("config", type=Path)
    codec_parser.add_argument("--frames", type=int, default=100)
    codec_parser.add_argument("--output-root", type=Path, help="Override codec-test output root.")

    session_pixel_parser = subparsers.add_parser(
        "verify-session-pixels",
        help="Verify a completed session's video decode/count integrity and pixel semantics.",
    )
    session_pixel_parser.add_argument("session_dir", type=Path)
    session_pixel_parser.add_argument(
        "--max-decode-frames",
        type=int,
        help="Decode only this many frames for a quick sample. Omit to decode all recorded frames.",
    )
    session_pixel_parser.add_argument(
        "--timeout-s",
        type=float,
        default=3600.0,
        help="Per-segment FFmpeg decode timeout in seconds.",
    )

    benchmark_parser = subparsers.add_parser(
        "benchmark-codecs",
        help="Benchmark synthetic frames through candidate codec settings for the selected camera geometry.",
    )
    benchmark_parser.add_argument("config", type=Path)
    benchmark_parser.add_argument("--frames", type=int, default=300)
    benchmark_parser.add_argument("--output-root", type=Path, help="Override benchmark output root.")
    benchmark_parser.add_argument(
        "--include-slow",
        action="store_true",
        help="Also test CPU-heavy exploratory codecs such as libx265, libsvtav1, and ProRes.",
    )

    make_config_parser = subparsers.add_parser(
        "make-camera-config",
        help="Generate a reviewable candidate parameter YAML for camera onboarding.",
    )
    make_config_parser.add_argument("base_config", type=Path)
    make_config_parser.add_argument("--output", type=Path)
    make_config_parser.add_argument("--output-dir", type=Path)
    make_config_parser.add_argument("--profile")
    make_config_parser.add_argument("--camera-make")
    make_config_parser.add_argument("--serial")
    make_config_parser.add_argument("--pfs-path", type=Path)
    make_config_parser.add_argument("--pixel-format", required=True)
    make_config_parser.add_argument("--expected-fps", type=float)
    make_config_parser.add_argument("--width", type=int)
    make_config_parser.add_argument("--height", type=int)
    make_config_parser.add_argument("--segment-seconds", type=float, default=120.0)
    make_config_parser.add_argument("--runtime-pixel-format-override", action="store_true", default=True)
    make_config_parser.add_argument("--no-runtime-pixel-format-override", dest="runtime_pixel_format_override", action="store_false")
    make_config_parser.add_argument("--runtime-frame-rate-override", action="store_true")

    lock_parser = subparsers.add_parser(
        "lock-profile",
        help="Create a hardware-specific approved config from passing validation evidence.",
    )
    lock_parser.add_argument("config", type=Path)
    lock_parser.add_argument("validation_summary", type=Path)
    lock_parser.add_argument("--preview-mode", choices=("off", "on"), required=True)
    lock_parser.add_argument("--output", type=Path, required=True)

    finalize_parser = subparsers.add_parser(
        "finalize-qualification",
        help="Compute the task-quality gate, update profile status, and create preview-on/off approved YAMLs.",
    )
    finalize_parser.add_argument("config", type=Path, help="Candidate profile YAML used for the sweep.")
    finalize_parser.add_argument("validation_summary", type=Path)
    finalize_parser.add_argument(
        "--task-quality-record",
        type=Path,
        help="Sweep-bound record; defaults to task_quality_record.json beside the summary.",
    )
    finalize_parser.add_argument(
        "--approved-dir",
        type=Path,
        default=Path("configs/approved"),
        help="Empty destination directory for the two approved YAMLs.",
    )
    finalize_parser.add_argument(
        "--reference-dataset",
        type=Path,
        help="Immutable task-evaluation dataset, archive, or manifest to hash into the record.",
    )
    finalize_parser.add_argument("--tracking-median-error-px", type=float)
    finalize_parser.add_argument("--segmentation-iou", type=float)
    finalize_parser.add_argument("--event-f1", type=float)

    sweep_parser = subparsers.add_parser(
        "validation-sweep",
        help="Run independent profile validation cases and write evidence files.",
    )
    sweep_parser.add_argument("args", nargs=argparse.REMAINDER)

    preflight_parser = subparsers.add_parser("preflight", help="Validate config, disk, and FFmpeg.")
    preflight_parser.add_argument("config", type=Path)
    preflight_parser.add_argument("--output-root", type=Path, help="Override session output root.")
    _add_preview_args(preflight_parser)
    _add_duration_args(preflight_parser)

    record_parser = subparsers.add_parser("record", help="Run a camera recording session.")
    record_parser.add_argument("config", type=Path)
    record_parser.add_argument("--fail-on-warning", action="store_true")
    record_parser.add_argument(
        "--allow-unspecified-metadata",
        action="store_true",
        help="Allow recording when experiment metadata fields are still UNSPECIFIED. Use only for engineering tests.",
    )
    record_parser.add_argument("--output-root", type=Path, help="Override session output root.")
    record_parser.add_argument(
        "--stop-file",
        type=Path,
        help=(
            "Optional file watched during recording. Creating this file requests a graceful stop "
            "without sending a console interrupt."
        ),
    )
    _add_preview_args(record_parser)
    _add_duration_args(record_parser)

    preview_parser = subparsers.add_parser("preview", help="Run camera live view without recording.")
    preview_parser.add_argument("config", type=Path)
    preview_parser.add_argument(
        "--stop-file",
        type=Path,
        help="Optional file watched during preview. Creating this file requests a graceful stop.",
    )
    preview_parser.add_argument("--max-seconds", type=float, help="Optional preview time limit.")
    preview_parser.add_argument("--output-root", type=Path, help="Override output root used for preview status files.")
    _add_preview_args(preview_parser)
    _add_duration_args(preview_parser)

    args = parser.parse_args(argv)

    if args.command == "gui":
        from .gui import main as gui_main

        return gui_main()

    if args.command == "devices":
        report = inspect_pylon()
        print(json.dumps(_jsonable(report.to_dict()), indent=2, sort_keys=True))
        return 0 if not report.error else 1

    if args.command == "profiles":
        print(json.dumps(_jsonable({"profiles": list_profile_dicts()}), indent=2, sort_keys=True))
        return 0

    if args.command == "camera-capabilities":
        report = inspect_camera_capabilities(serial=args.serial)
        print(json.dumps(_jsonable(report.to_dict()), indent=2, sort_keys=True))
        return 0 if not report.error else 1

    if args.command == "report":
        report = build_session_report(args.session_dir)
        rendered = json.dumps(_jsonable(report), indent=2, sort_keys=True)
        if args.output is not None:
            output_path = args.output.expanduser().resolve()
            if output_path.exists():
                print(f"ERROR: Refusing to overwrite existing report: {output_path}")
                return 2
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(rendered + "\n", encoding="utf-8")
            except OSError as exc:
                print(f"ERROR: Could not write report to {output_path}: {exc}")
                return 2
        print(rendered)
        return 0

    if args.command == "index":
        result = write_session_index(
            args.output_root,
            args.output,
            output_format=args.format,
            include_qc=args.include_qc,
        )
        print(json.dumps(_jsonable(result), indent=2, sort_keys=True))
        return 0

    if args.command == "qualification-plan":
        try:
            result = build_qualification_plan(args.config)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 2
        print(json.dumps(_jsonable(result), indent=2, sort_keys=True))
        return 0

    if args.command == "read-video":
        try:
            report = inspect_video_read(args.video, mode=args.mode, frame_index=args.frame)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 2
        print(json.dumps(_jsonable(report.to_dict()), indent=2, sort_keys=True))
        return 0

    if args.command == "export-analysis":
        try:
            report = export_session_for_analysis(
                args.session_dir,
                output_dir=args.output_dir,
                mode=args.mode,
                encoder=args.encoder,
                crf=args.crf,
                qp=args.qp,
                max_frames=args.max_frames,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 2
        print(json.dumps(_jsonable(report.to_dict()), indent=2, sort_keys=True))
        return 0

    if args.command == "verify-codec":
        try:
            cfg = load_config(args.config, duration_s=1.0, output_root=getattr(args, "output_root", None))
            output_root = getattr(args, "output_root", None) or cfg.session.output_root
            report = verify_lossless_codec(cfg, Path(output_root), frames=args.frames)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 2
        print(json.dumps(_jsonable(report.to_dict()), indent=2, sort_keys=True))
        return 0

    if args.command == "verify-session-pixels":
        try:
            report = verify_session_pixels(
                args.session_dir,
                max_decode_frames=getattr(args, "max_decode_frames", None),
                timeout_s=float(getattr(args, "timeout_s", 3600.0)),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 2
        print(json.dumps(_jsonable(report.to_dict()), indent=2, sort_keys=True))
        return 0 if not report.issues and not report.acquisition_issues else 1

    if args.command == "benchmark-codecs":
        try:
            cfg = load_config(args.config, duration_s=1.0, output_root=getattr(args, "output_root", None))
            output_root = getattr(args, "output_root", None) or cfg.session.output_root
            reports = benchmark_codecs(
                cfg,
                Path(output_root),
                frames=args.frames,
                include_slow=bool(getattr(args, "include_slow", False)),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 2
        print(json.dumps(_jsonable({"benchmarks": [report.to_dict() for report in reports]}), indent=2, sort_keys=True))
        return 0

    if args.command == "make-camera-config":
        try:
            report = generate_camera_config(
                args.base_config,
                output_path=args.output,
                output_dir=args.output_dir,
                profile=args.profile,
                camera_make=args.camera_make,
                serial=args.serial,
                pfs_path=args.pfs_path,
                pixel_format=args.pixel_format,
                expected_fps=args.expected_fps,
                width=args.width,
                height=args.height,
                segment_seconds=args.segment_seconds,
                runtime_pixel_format_override=args.runtime_pixel_format_override,
                runtime_frame_rate_override=args.runtime_frame_rate_override,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 2
        print(json.dumps(_jsonable(report.to_dict()), indent=2, sort_keys=True))
        return 0

    if args.command == "validation-sweep":
        from .validation_sweep import main as validation_sweep_main

        return validation_sweep_main(args.args)

    if args.command == "lock-profile":
        try:
            result = lock_profile_from_summary(
                args.config,
                args.validation_summary,
                args.output,
                preview_mode=args.preview_mode,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 2
        print(json.dumps(_jsonable(result), indent=2, sort_keys=True))
        return 0

    if args.command == "finalize-qualification":
        task_record = args.task_quality_record or args.validation_summary.parent / "task_quality_record.json"
        measurement_values = (
            args.reference_dataset,
            args.tracking_median_error_px,
            args.segmentation_iou,
            args.event_f1,
        )
        if any(value is not None for value in measurement_values) and not all(
            value is not None for value in measurement_values
        ):
            print(
                "ERROR: Provide --reference-dataset and all three task metrics together, or omit all four."
            )
            return 2
        try:
            result = finalize_qualification_from_summary(
                args.config,
                args.validation_summary,
                task_record,
                args.approved_dir,
                reference_dataset_path=args.reference_dataset,
                task_metrics=(
                    {
                        "tracking_median_error_px": args.tracking_median_error_px,
                        "segmentation_iou": args.segmentation_iou,
                        "event_f1": args.event_f1,
                    }
                    if args.reference_dataset is not None
                    else None
                ),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 2
        print(json.dumps(_jsonable(result), indent=2, sort_keys=True))
        return 0

    try:
        cfg = load_config(
            args.config,
            duration_s=(_duration_override_s(args) or 1.0) if args.command == "preview" else _duration_override_s(args),
            output_root=getattr(args, "output_root", None),
            preview_enabled=True if args.command == "preview" else getattr(args, "preview_enabled", None),
            preview_width=getattr(args, "preview_width", None),
            preview_max_fps=getattr(args, "preview_fps", None),
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2

    if args.command == "preflight":
        try:
            report = run_preflight(cfg)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 2
        print(json.dumps(_jsonable(asdict(report)), indent=2, sort_keys=True))
        return 0

    if args.command == "preview":
        cfg = load_config(
            args.config,
            duration_s=_duration_override_s(args) or 1.0,
            output_root=getattr(args, "output_root", None),
            preview_enabled=True,
            preview_width=getattr(args, "preview_width", None),
            preview_max_fps=getattr(args, "preview_fps", None),
        )
        try:
            stats = PreviewSession(
                cfg,
                stop_file=getattr(args, "stop_file", None),
                max_seconds=getattr(args, "max_seconds", None),
            ).run()
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 2
        print(json.dumps(_jsonable(stats_to_dict(stats)), indent=2, sort_keys=True))
        return 0

    if args.command == "record":
        metadata_issues = cfg.experiment.readiness_issues()
        if metadata_issues and not args.allow_unspecified_metadata:
            print(
                "ERROR: Experiment metadata is incomplete or invalid: "
                + "; ".join(metadata_issues)
                + ". For engineering-only tests, rerun with --allow-unspecified-metadata."
            )
            return 1
        try:
            report = run_preflight(cfg)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 2
        if report.warnings:
            for warning in report.warnings:
                print(f"WARNING: {warning}")
            if args.fail_on_warning:
                return 1
        stats = Recorder(cfg, report, stop_file=getattr(args, "stop_file", None)).run()
        for warning in _recording_stats_warnings(stats):
            print(f"WARNING: {warning}")
        print(json.dumps(_jsonable(asdict(stats)), indent=2, sort_keys=True))
        return 0

    parser.error(f"Unsupported command: {args.command}")
    return 2


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


def _add_duration_args(parser: argparse.ArgumentParser) -> None:
    duration_group = parser.add_mutually_exclusive_group()
    duration_group.add_argument("--duration-s", type=float, help="Override session duration in seconds.")
    duration_group.add_argument("--duration-min", type=float, help="Override session duration in minutes.")


def _add_preview_args(parser: argparse.ArgumentParser) -> None:
    preview_group = parser.add_mutually_exclusive_group()
    preview_group.add_argument("--preview", dest="preview_enabled", action="store_true", help="Show best-effort live preview.")
    preview_group.add_argument("--no-preview", dest="preview_enabled", action="store_false", help="Disable live preview.")
    parser.set_defaults(preview_enabled=None)
    parser.add_argument("--preview-width", type=int, help="Preview display width in pixels.")
    parser.add_argument("--preview-fps", type=float, help="Maximum preview display FPS.")


def _duration_override_s(args: argparse.Namespace) -> float | None:
    if getattr(args, "duration_s", None) is not None:
        return float(args.duration_s)
    if getattr(args, "duration_min", None) is not None:
        return float(args.duration_min) * 60.0
    return None


def _normalize_duration_shorthand(argv: list[str]) -> list[str]:
    normalized: list[str] = []
    for arg in argv:
        match = re.fullmatch(r"--([1-9][0-9]*(?:\.[0-9]+)?)", arg)
        if match:
            normalized.extend(["--duration-s", match.group(1)])
        else:
            normalized.append(arg)
    return normalized


def _recording_stats_warnings(stats: Any) -> list[str]:
    if not is_dataclass(stats):
        return []
    values = {field.name: getattr(stats, field.name) for field in fields(stats)}
    warnings = []
    expected = values.get("expected_frames") or 0
    grabbed = values.get("frames_grabbed") or 0
    written = values.get("frames_written") or 0
    max_queue = values.get("max_queue_depth") or 0
    queue_capacity = values.get("queue_capacity_frames") or 0
    queue_pressure = values.get("queue_pressure_events") or 0
    queue_full = values.get("queue_full_errors") or 0
    dropped = values.get("dropped_detected") or 0
    preview_error = values.get("preview_error") or ""

    if expected and (grabbed != expected or written != expected):
        warnings.append(
            f"Frame count incomplete: grabbed={grabbed}, written={written}, expected={expected}."
        )
    if dropped:
        warnings.append(f"Detected dropped-frame estimate is nonzero: {dropped}.")
    if queue_full:
        warnings.append(f"Writer queue filled {queue_full} time(s). This profile is not real-time safe.")
    queue_warning_threshold = int(queue_capacity * 0.90) if queue_capacity else 512
    if max_queue >= queue_warning_threshold or queue_pressure:
        warnings.append(
            f"Writer queue pressure was high: max_depth={max_queue}, pressure_events={queue_pressure}. "
            "Run pycamrec report to check real-time timing."
        )
    if preview_error:
        warnings.append(f"Preview ended with an error: {preview_error}")
    return warnings
