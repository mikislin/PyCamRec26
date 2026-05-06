"""Command line interface."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any

from .acquisition import Recorder
from .config import load_config
from .diagnostics import inspect_pylon
from .preflight import run_preflight
from .preview_session import PreviewSession, stats_to_dict
from .profiles import list_profile_dicts
from .report import build_session_report
from .verify import verify_lossless_codec


def main(argv: list[str] | None = None) -> int:
    argv = _normalize_duration_shorthand(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="pycamrec")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("devices", help="Show what pypylon can enumerate.")
    subparsers.add_parser("gui", help="Launch the PyCamRec Windows desktop app.")
    subparsers.add_parser("profiles", help="List built-in recording profiles.")

    report_parser = subparsers.add_parser("report", help="Summarize a completed PyCamRec session.")
    report_parser.add_argument("session_dir", type=Path)

    codec_parser = subparsers.add_parser(
        "verify-codec",
        help="Verify that a profile's encoder settings round-trip synthetic gray frames losslessly.",
    )
    codec_parser.add_argument("config", type=Path)
    codec_parser.add_argument("--frames", type=int, default=100)
    codec_parser.add_argument("--output-root", type=Path, help="Override codec-test output root.")

    preflight_parser = subparsers.add_parser("preflight", help="Validate config, disk, and FFmpeg.")
    preflight_parser.add_argument("config", type=Path)
    preflight_parser.add_argument("--output-root", type=Path, help="Override session output root.")
    _add_preview_args(preflight_parser)
    _add_duration_args(preflight_parser)

    record_parser = subparsers.add_parser("record", help="Run a Basler recording session.")
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

    if args.command == "report":
        report = build_session_report(args.session_dir)
        print(json.dumps(_jsonable(report), indent=2, sort_keys=True))
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
        missing_metadata = cfg.experiment.missing_fields()
        if missing_metadata and not args.allow_unspecified_metadata:
            print(
                "ERROR: Experiment metadata is incomplete. Fill these experiment fields before recording: "
                + ", ".join(missing_metadata)
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
