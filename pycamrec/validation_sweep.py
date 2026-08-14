"""Independent validation sweep runner for new PyCamRec hardware/settings."""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import load_config
from .hardware import session_hardware_fingerprint
from .report import build_session_report
from .profiles import profile_claims_losslessness
from .verify import verify_session_pixels


SESSION_RE = re.compile(
    r"(?:Finalized|Interrupted session finalized|Stopped at)\s+(.+?)\.\s+Camera",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SweepCase:
    case_id: str
    duration_s: float
    bitrate_mbps: float
    bitrate_override: bool
    preview_enabled: bool
    preview_width: int
    preview_fps: float
    repeat: int
    segment_seconds: float


@dataclass(frozen=True)
class SweepResult:
    case_id: str
    duration_s: float
    bitrate_mbps: float
    bitrate_override: bool
    preview_enabled: bool
    session_dir: str
    return_code: int
    qc_status: str
    scientific_pass: bool
    acquisition_pass: bool
    qc_pass: bool
    frames: int | None
    expected_frames: int | None
    observed_fps: float | None
    max_queue_depth: int | None
    queue_capacity: int | None
    queue_fraction: float | None
    preferred_queue_pass: bool | None
    block_id_gaps: int | None
    drop_sum: int | None
    metadata_complete: bool | None
    segment_count: int | None
    rollover_pass: bool
    clean_finalization_pass: bool
    segment_mbps: float | None
    ffprobe_frames: int | None
    pixel_status: str
    pixel_verified: bool
    pixel_pass: bool
    profile_id: str
    pixel_fidelity: str
    lossless_claim: bool
    hardware_fingerprint_sha256: str
    technical_pass: bool
    evidence_ready: bool
    profile_approval_pass: bool
    experiment_ready: bool
    error: str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pycamrec-validation-sweep",
        description="Run a parameter sweep using normal pycamrec record/report commands.",
    )
    parser.add_argument("config", type=Path, help="Base PyCamRec YAML config.")
    parser.add_argument("--output-root", type=Path, default=Path("D:/PyCamRecSessions"))
    parser.add_argument("--sweep-dir", type=Path, default=Path("validation_sweeps"))
    parser.add_argument("--durations", default="60", help="Comma-separated seconds, e.g. 30,60,600.")
    parser.add_argument("--bitrates", default="", help="Comma-separated Mbps. Empty keeps the base profile bitrate.")
    parser.add_argument("--preview", choices=("off", "on", "both"), default="off")
    parser.add_argument(
        "--preview-order",
        choices=("on-first", "off-first"),
        default="on-first",
        help="Case order when --preview both is used. Default stresses live preview first.",
    )
    parser.add_argument("--preview-width", type=int, default=512)
    parser.add_argument("--preview-fps", type=float, default=10.0)
    parser.add_argument(
        "--segment-seconds",
        type=float,
        help="Validation segment length. Default is at most half of each case so rollover is exercised.",
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--min-fps-ratio", type=float, default=0.98)
    parser.add_argument("--max-queue-fraction", type=float, default=0.90)
    parser.add_argument("--preferred-max-queue-fraction", type=float, default=0.25)
    parser.add_argument(
        "--verify-session-pixels",
        action="store_true",
        help="Run pycamrec's real-session pixel/decode verification after each successful case.",
    )
    parser.add_argument(
        "--pixel-max-decode-frames",
        type=int,
        help="Limit decoded frames for session pixel verification. Omit for full decode.",
    )
    parser.add_argument(
        "--require-complete-metadata",
        action="store_true",
        help="Compatibility flag; evidence and experiment readiness always require complete metadata.",
    )
    parser.add_argument(
        "--source-frame-hash-every",
        type=int,
        default=0,
        help="Validation-only: record source frame MD5 every N frames. 0 disables hashing.",
    )
    parser.add_argument(
        "--source-frame-hash-max-frames",
        type=int,
        default=0,
        help="Validation-only: maximum source hashes to write. 0 means no limit.",
    )
    parser.add_argument("--allow-unspecified-metadata", action="store_true")
    parser.add_argument("--stop-on-fail", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    base_cfg = load_config(args.config, duration_s=1.0, output_root=args.output_root)
    parsed_bitrates = _parse_float_list(args.bitrates)
    cases = _build_cases(
        durations=_parse_float_list(args.durations),
        bitrates=parsed_bitrates or [base_cfg.writer.expected_bitrate_mbps or base_cfg.recording_profile.expected_bitrate_mbps or 0.0],
        bitrate_override=bool(parsed_bitrates),
        preview=args.preview,
        preview_order=args.preview_order,
        preview_width=args.preview_width,
        preview_fps=args.preview_fps,
        repeats=args.repeats,
        base_segment_seconds=base_cfg.writer.segment_seconds,
        segment_seconds=args.segment_seconds,
    )
    if args.dry_run:
        print(json.dumps({"cases": [asdict(case) for case in cases]}, indent=2, sort_keys=True))
        return 0

    sweep_dir = args.sweep_dir.expanduser().resolve() / datetime.now().strftime("%Y%m%d_%H%M%S")
    sweep_dir.mkdir(parents=True, exist_ok=False)
    runtime_config = sweep_dir / "runtime_current.yaml"
    json_path = sweep_dir / "validation_sweep.json"
    csv_path = sweep_dir / "validation_sweep.csv"

    results: list[SweepResult] = []
    for case in cases:
        _write_runtime_config(
            args.config,
            runtime_config,
            case,
            args.output_root,
            source_frame_hash_every=args.source_frame_hash_every,
            source_frame_hash_max_frames=args.source_frame_hash_max_frames,
        )
        runtime_cfg = load_config(runtime_config)
        timeout_padding_s = max(900.0, runtime_cfg.writer.finalize_timeout_s + 300.0)
        result = _run_case(
            runtime_config,
            case,
            allow_unspecified_metadata=args.allow_unspecified_metadata,
            min_fps_ratio=args.min_fps_ratio,
            max_queue_fraction=args.max_queue_fraction,
            preferred_max_queue_fraction=args.preferred_max_queue_fraction,
            verify_pixels=args.verify_session_pixels,
            pixel_max_decode_frames=args.pixel_max_decode_frames,
            require_complete_metadata=args.require_complete_metadata,
            timeout_s=max(300.0, case.duration_s + timeout_padding_s),
        )
        results.append(result)
        _write_results(json_path, csv_path, args.config, cases, results)
        print(json.dumps(asdict(result), sort_keys=True), flush=True)
        if result.return_code == 130:
            return 130
        if args.stop_on_fail and not result.qc_pass:
            break

    _write_summary(
        sweep_dir,
        args.config,
        results,
        min_fps_ratio=args.min_fps_ratio,
        max_queue_fraction=args.max_queue_fraction,
        preferred_max_queue_fraction=args.preferred_max_queue_fraction,
        require_complete_metadata=args.require_complete_metadata,
        verify_pixels=args.verify_session_pixels,
    )

    return 0 if any(result.qc_pass for result in results) else 1


def _build_cases(
    *,
    durations: list[float],
    bitrates: list[float],
    bitrate_override: bool,
    preview: str,
    preview_order: str,
    preview_width: int,
    preview_fps: float,
    repeats: int,
    base_segment_seconds: float,
    segment_seconds: float | None,
) -> list[SweepCase]:
    if preview == "both":
        preview_values = [True, False] if preview_order == "on-first" else [False, True]
    else:
        preview_values = [preview == "on"]
    cases = []
    for duration in durations:
        rollover_seconds = float(
            segment_seconds
            if segment_seconds is not None
            else min(base_segment_seconds, max(0.1, duration / 2.0))
        )
        if rollover_seconds <= 0:
            raise ValueError("segment_seconds must be positive.")
        for bitrate in bitrates:
            for preview_enabled in preview_values:
                for repeat in range(1, repeats + 1):
                    bitrate_label = f"b{bitrate:g}" if bitrate_override else "native"
                    case_id = (
                        f"d{duration:g}_{bitrate_label}_"
                        f"preview{'on' if preview_enabled else 'off'}_r{repeat}"
                    )
                    cases.append(
                        SweepCase(
                            case_id=case_id,
                            duration_s=duration,
                            bitrate_mbps=bitrate,
                            bitrate_override=bitrate_override,
                            preview_enabled=preview_enabled,
                            preview_width=preview_width,
                            preview_fps=preview_fps,
                            repeat=repeat,
                            segment_seconds=rollover_seconds,
                        )
                    )
    return cases


def _write_runtime_config(
    base_path: Path,
    runtime_path: Path,
    case: SweepCase,
    output_root: Path,
    *,
    source_frame_hash_every: int,
    source_frame_hash_max_frames: int,
) -> None:
    import yaml

    data = yaml.safe_load(base_path.expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a mapping: {base_path}")
    data = dict(data)
    base_dir = base_path.expanduser().resolve().parent
    camera = dict(data.get("camera", {}))
    if camera.get("pfs_path"):
        pfs_path = Path(str(camera["pfs_path"])).expanduser()
        if not pfs_path.is_absolute():
            pfs_path = (base_dir / pfs_path).resolve()
        camera["pfs_path"] = str(pfs_path)
    data["camera"] = camera

    session = dict(data.get("session", {}))
    session["duration_s"] = case.duration_s
    session["output_root"] = str(output_root)
    session["name"] = f"{session.get('name', 'session')}_sweep_{case.case_id}"
    data["session"] = session

    preview = dict(data.get("preview", {}))
    preview["enabled"] = case.preview_enabled
    preview["width"] = case.preview_width
    preview["max_fps"] = case.preview_fps
    data["preview"] = preview

    writer = dict(data.get("writer", {}))
    writer["segment_seconds"] = case.segment_seconds
    data["writer"] = writer

    if source_frame_hash_every > 0 or source_frame_hash_max_frames > 0:
        metadata = dict(data.get("metadata", {}))
        metadata["source_frame_hash_every"] = source_frame_hash_every
        metadata["source_frame_hash_max_frames"] = source_frame_hash_max_frames
        data["metadata"] = metadata

    if case.bitrate_override and case.bitrate_mbps > 0:
        cfg = load_config(base_path, duration_s=case.duration_s, output_root=output_root)
        writer = dict(data.get("writer", {}))
        writer["expected_bitrate_mbps"] = case.bitrate_mbps
        writer["output_args"] = _h264_nvenc_cbr_args(
            bitrate_mbps=case.bitrate_mbps,
            fps=cfg.camera.expected_fps,
            output_pix_fmt=cfg.writer.output_pix_fmt,
        )
        data["writer"] = writer

    runtime_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _run_case(
    runtime_config: Path,
    case: SweepCase,
    *,
    allow_unspecified_metadata: bool,
    min_fps_ratio: float,
    max_queue_fraction: float,
    preferred_max_queue_fraction: float,
    verify_pixels: bool,
    pixel_max_decode_frames: int | None,
    require_complete_metadata: bool,
    timeout_s: float,
) -> SweepResult:
    command = [
        sys.executable,
        "-m",
        "pycamrec",
        "record",
        str(runtime_config),
        "--no-preview" if not case.preview_enabled else "--preview",
    ]
    stop_file = runtime_config.with_name(f"stop_{case.case_id}.txt")
    try:
        stop_file.unlink()
    except FileNotFoundError:
        pass
    command.extend(["--stop-file", str(stop_file)])
    if allow_unspecified_metadata:
        command.append("--allow-unspecified-metadata")

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        _request_child_stop(stop_file, "timeout")
        try:
            stdout, stderr = process.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        return _failed_result(
            case,
            return_code=124,
            qc_status="timeout",
            error=f"timeout after {timeout_s:g}s: {exc}\n{((stdout or '') + chr(10) + (stderr or ''))[-4000:]}",
        )
    except KeyboardInterrupt:
        _request_child_stop(stop_file, "keyboard_interrupt")
        try:
            stdout, stderr = process.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        output = (stdout or "") + "\n" + (stderr or "")
        return _failed_result(
            case,
            return_code=130,
            qc_status="interrupted",
            session_dir=_parse_session_dir(output),
            error=output[-4000:],
        )
    finally:
        try:
            stop_file.unlink()
        except FileNotFoundError:
            pass

    output = (stdout or "") + "\n" + (stderr or "")
    session_dir = _parse_session_dir(output)
    return_code = process.returncode if process.returncode is not None else 1
    if return_code != 0 or not session_dir:
        return _failed_result(
            case,
            return_code=return_code,
            qc_status="record_failed",
            session_dir=session_dir,
            error=output[-4000:],
        )

    report = build_session_report(Path(session_dir))
    qc = report.get("qc") or {}
    frames = report.get("frames") or {}
    queue = report.get("queue") or {}
    segments = report.get("segments") or {}
    session_state = report.get("session_state") or {}
    profile = report.get("recording_profile") or {}
    profile_approval = report.get("profile_approval") or {}
    writer = _read_resolved_writer(Path(session_dir))
    queue_capacity = _to_int(writer.get("queue_max_frames"))
    observed_fps = _to_float(qc.get("observed_host_fps"))
    expected_fps = _to_float(qc.get("expected_fps"))
    max_queue = _to_int(queue.get("max_depth"))
    queue_fraction = (
        max_queue / queue_capacity
        if max_queue is not None and queue_capacity not in (None, 0)
        else None
    )
    ffprobe_frames = _ffprobe_total_frames(Path(session_dir), str(writer.get("ffprobe_path") or "ffprobe"))
    expected_frame_count = _to_int(frames.get("expected"))
    frame_count = _to_int(frames.get("count"))
    block_id_gaps = _to_int(frames.get("block_id_gaps"))
    drop_sum = _to_int(frames.get("drop_sum"))
    metadata_report = report.get("experiment_metadata") or {}
    metadata_complete = bool(metadata_report.get("metadata_complete"))
    segment_count = _to_int(segments.get("count"))
    clean_finalization_pass = bool(session_state.get("clean_finalization"))
    rollover_pass = segment_count is not None and segment_count >= 2
    profile_id = str(profile.get("id") or "")
    pixel_fidelity = str(profile.get("pixel_fidelity") or "")
    lossless_claim = profile_claims_losslessness(pixel_fidelity)
    pixel_status = "not_run"
    pixel_verified = False
    pixel_error = ""
    if verify_pixels:
        try:
            pixel_report = verify_session_pixels(
                Path(session_dir),
                max_decode_frames=pixel_max_decode_frames,
            )
            pixel_status = pixel_report.status
            pixel_verified = bool(
                not pixel_report.issues
                and pixel_report.hashes_match
                and pixel_report.status in {"pass_exact_pixels", "pass_exact_pixel_sample"}
            )
        except Exception as exc:
            pixel_status = "error"
            pixel_error = repr(exc)
    hardware_fingerprint = session_hardware_fingerprint(Path(session_dir))
    acquisition_pass = (
        return_code == 0
        and bool(qc.get("acquisition_pass"))
        and frame_count is not None
        and expected_frame_count is not None
        and frame_count == expected_frame_count
        and (block_id_gaps or 0) == 0
        and (drop_sum or 0) == 0
        and (ffprobe_frames is not None and ffprobe_frames == expected_frame_count)
        and clean_finalization_pass
        and rollover_pass
    )
    rate_pass = bool(
        observed_fps is not None
        and expected_fps is not None
        and observed_fps >= expected_fps * min_fps_ratio
    )
    queue_pass = bool(
        max_queue is not None
        and queue_capacity is not None
        and max_queue < queue_capacity * max_queue_fraction
    )
    qc_pass = acquisition_pass and rate_pass and queue_pass and qc.get("status") == "pass"
    technical_pass = qc_pass
    pixel_pass = pixel_verified if lossless_claim else True
    evidence_ready = qc_pass and pixel_pass and metadata_complete
    profile_approval_pass = bool(profile_approval.get("approved"))
    experiment_ready = evidence_ready and profile_approval_pass
    scientific_pass = experiment_ready
    return SweepResult(
        case_id=case.case_id,
        duration_s=case.duration_s,
        bitrate_mbps=case.bitrate_mbps,
        bitrate_override=case.bitrate_override,
        preview_enabled=case.preview_enabled,
        session_dir=session_dir,
        return_code=return_code,
        qc_status=str(qc.get("status") or "unknown"),
        scientific_pass=scientific_pass,
        acquisition_pass=acquisition_pass,
        qc_pass=qc_pass,
        frames=frame_count,
        expected_frames=expected_frame_count,
        observed_fps=observed_fps,
        max_queue_depth=max_queue,
        queue_capacity=queue_capacity,
        queue_fraction=queue_fraction,
        preferred_queue_pass=(
            max_queue < queue_capacity * preferred_max_queue_fraction
            if max_queue is not None and queue_capacity is not None
            else None
        ),
        block_id_gaps=block_id_gaps,
        drop_sum=drop_sum,
        metadata_complete=metadata_complete,
        segment_count=segment_count,
        rollover_pass=rollover_pass,
        clean_finalization_pass=clean_finalization_pass,
        segment_mbps=_to_float(segments.get("approx_mbps")),
        ffprobe_frames=ffprobe_frames,
        pixel_status=pixel_status,
        pixel_verified=pixel_verified,
        pixel_pass=pixel_pass,
        profile_id=profile_id,
        pixel_fidelity=pixel_fidelity,
        lossless_claim=lossless_claim,
        hardware_fingerprint_sha256=str(hardware_fingerprint.get("fingerprint_sha256") or ""),
        technical_pass=technical_pass,
        evidence_ready=evidence_ready,
        profile_approval_pass=profile_approval_pass,
        experiment_ready=experiment_ready,
        error=pixel_error,
    )


def _h264_nvenc_cbr_args(*, bitrate_mbps: float, fps: float, output_pix_fmt: str) -> list[str]:
    bitrate = f"{bitrate_mbps:g}M"
    return [
        "-c:v",
        "h264_nvenc",
        "-preset",
        "p1",
        "-tune",
        "ull",
        "-rc",
        "cbr",
        "-b:v",
        bitrate,
        "-maxrate",
        bitrate,
        "-bufsize",
        f"{max(1.0, bitrate_mbps / 2.0):g}M",
        "-bf",
        "0",
        "-g",
        str(max(1, int(round(fps * 6)))),
        "-surfaces",
        "64",
        "-zerolatency",
        "1",
        "-gpu",
        "0",
        "-pix_fmt",
        output_pix_fmt,
        "-color_range",
        "pc",
    ]


def _failed_result(
    case: SweepCase,
    *,
    return_code: int,
    qc_status: str,
    error: str,
    session_dir: str = "",
) -> SweepResult:
    return SweepResult(
        case_id=case.case_id,
        duration_s=case.duration_s,
        bitrate_mbps=case.bitrate_mbps,
        bitrate_override=case.bitrate_override,
        preview_enabled=case.preview_enabled,
        session_dir=session_dir,
        return_code=return_code,
        qc_status=qc_status,
        scientific_pass=False,
        acquisition_pass=False,
        qc_pass=False,
        frames=None,
        expected_frames=None,
        observed_fps=None,
        max_queue_depth=None,
        queue_capacity=None,
        queue_fraction=None,
        preferred_queue_pass=None,
        block_id_gaps=None,
        drop_sum=None,
        metadata_complete=None,
        segment_count=None,
        rollover_pass=False,
        clean_finalization_pass=False,
        segment_mbps=None,
        ffprobe_frames=None,
        pixel_status="not_run",
        pixel_verified=False,
        pixel_pass=False,
        profile_id="",
        pixel_fidelity="",
        lossless_claim=False,
        hardware_fingerprint_sha256="",
        technical_pass=False,
        evidence_ready=False,
        profile_approval_pass=False,
        experiment_ready=False,
        error=error,
    )


def _parse_session_dir(output: str) -> str:
    matches = SESSION_RE.findall(output)
    return matches[-1].strip() if matches else ""


def _request_child_stop(stop_file: Path, reason: str) -> None:
    try:
        stop_file.write_text(reason, encoding="utf-8")
    except OSError:
        pass


def _read_resolved_writer(session_dir: Path) -> dict[str, Any]:
    session = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    return ((session.get("resolved") or {}).get("writer") or {})


def _ffprobe_total_frames(session_dir: Path, ffprobe_path: str) -> int | None:
    segments_path = session_dir / "segments.csv"
    if not segments_path.is_file():
        return None
    total = 0
    found = False
    with segments_path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            segment_path = Path(row.get("path") or "")
            if not segment_path.is_absolute():
                segment_path = session_dir / segment_path
            try:
                completed = subprocess.run(
                    [
                        ffprobe_path,
                        "-v",
                        "error",
                        "-select_streams",
                        "v:0",
                        "-show_entries",
                        "stream=nb_frames",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        str(segment_path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except Exception:
                return None
            value = _to_int(completed.stdout.strip())
            if value is None:
                return None
            total += value
            found = True
    return total if found else None


def _write_results(json_path: Path, csv_path: Path, base_config: Path, cases: list[SweepCase], results: list[SweepResult]) -> None:
    document = {
        "base_config": str(base_config),
        "cases": [asdict(case) for case in cases],
        "results": [asdict(result) for result in results],
        "best_scientific_pass": _best_result(results),
    }
    json_path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(results[0]).keys()) if results else ["case_id"])
        writer.writeheader()
        for result in results:
            writer.writerow(asdict(result))


def _write_summary(
    sweep_dir: Path,
    base_config: Path,
    results: list[SweepResult],
    *,
    min_fps_ratio: float,
    max_queue_fraction: float,
    preferred_max_queue_fraction: float,
    require_complete_metadata: bool,
    verify_pixels: bool,
) -> None:
    fingerprints = sorted({result.hardware_fingerprint_sha256 for result in results if result.hardware_fingerprint_sha256})
    technical_passes = [result for result in results if result.technical_pass]
    acquisition_passes = [result for result in results if result.acquisition_pass]
    qc_passes = [result for result in results if result.qc_pass]
    scientific_passes = [result for result in results if result.scientific_pass]
    evidence_ready_results = [result for result in results if result.evidence_ready]
    preferred_passes = [
        result
        for result in evidence_ready_results
        if result.preferred_queue_pass is True
    ]
    document = {
        "schema_version": 1,
        "base_config": str(base_config),
        "hardware_fingerprint_sha256_values": fingerprints,
        "requirements": {
            "frame_count_equals_expected": True,
            "ffprobe_count_equals_expected": True,
            "block_id_gaps": 0,
            "drop_sum": 0,
            "observed_fps_min_ratio": min_fps_ratio,
            "max_queue_fraction_fail": max_queue_fraction,
            "max_queue_fraction_preferred": preferred_max_queue_fraction,
            "pixel_verification_required": verify_pixels,
            "metadata_complete_required": True,
        },
        "case_count": len(results),
        "acquisition_pass_count": len(acquisition_passes),
        "qc_pass_count": len(qc_passes),
        "technical_pass_count": len(technical_passes),
        "scientific_pass_count": len(scientific_passes),
        "evidence_ready_count": len(evidence_ready_results),
        "preferred_pass_count": len(preferred_passes),
        "profile_lock_recommendation": _profile_lock_recommendation(results, verify_pixels, require_complete_metadata),
        "profile_lock_by_preview_mode": {
            "preview_off": _profile_lock_recommendation(
                [result for result in results if not result.preview_enabled],
                verify_pixels,
                require_complete_metadata,
            ),
            "preview_on": _profile_lock_recommendation(
                [result for result in results if result.preview_enabled],
                verify_pixels,
                require_complete_metadata,
            ),
        },
        "best_scientific_pass": _best_result(scientific_passes),
        "best_profile_lock_candidate": _best_result(preferred_passes, readiness_field="evidence_ready"),
        "results": [asdict(result) for result in results],
    }
    (sweep_dir / "validation_summary.json").write_text(
        json.dumps(document, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _profile_lock_recommendation(
    results: list[SweepResult],
    verify_pixels: bool,
    require_complete_metadata: bool,
) -> str:
    if not results:
        return "no_cases_run"
    if len({result.preview_enabled for result in results}) != 1:
        return "do_not_lock_mixed_preview_modes_use_mode_specific_recommendations"
    if any(not result.acquisition_pass for result in results):
        return "do_not_lock_profile_acquisition_or_rollover_failed"
    if any(not result.qc_pass for result in results):
        return "do_not_lock_profile_some_cases_failed"
    if any(not result.clean_finalization_pass or not result.rollover_pass for result in results):
        return "do_not_lock_profile_rollover_or_finalization_failed"
    if any(result.lossless_claim and not result.pixel_verified for result in results):
        return "do_not_lock_profile_pixel_verification_missing_or_failed"
    if any(not result.metadata_complete for result in results):
        return "do_not_lock_profile_metadata_incomplete"
    fingerprints = {result.hardware_fingerprint_sha256 for result in results if result.hardware_fingerprint_sha256}
    if len(fingerprints) != 1:
        return "do_not_lock_profile_mixed_or_missing_hardware_fingerprint"
    if any(result.preferred_queue_pass is not True for result in results):
        return "do_not_lock_profile_queue_above_25_percent_margin"
    mode = "preview_on" if results[0].preview_enabled else "preview_off"
    return f"lock_validated_for_this_evidence_fingerprint_{mode}"


def _best_result(
    results: list[SweepResult],
    *,
    readiness_field: str = "scientific_pass",
) -> dict[str, Any] | None:
    passing = [result for result in results if bool(getattr(result, readiness_field))]
    if not passing:
        return None
    return asdict(
        sorted(
            passing,
            key=lambda item: (
                -item.duration_s,
                item.preview_enabled,
                item.bitrate_mbps,
                item.max_queue_depth or 0,
            ),
        )[0]
    )


def _parse_float_list(text: str) -> list[float]:
    return [float(item.strip()) for item in text.split(",") if item.strip()]


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
