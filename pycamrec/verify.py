"""Post-recording verification helpers."""

from __future__ import annotations

import csv
import json
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .pixel_formats import channel_semantics
from .report import build_session_report
from .schemas import PyCamRecConfig


def count_frame_rows(session_dir: Path) -> int:
    frames_path = session_dir / "frames.csv"
    with frames_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return sum(1 for _ in reader)


def ffprobe_video_frames(
    ffprobe_path: str,
    video_path: Path,
    *,
    timeout_s: float = 60.0,
    count_frames: bool = True,
) -> int | None:
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
    ]
    if count_frames:
        command.append("-count_frames")
    command.extend(
        [
        "-show_entries",
        "stream=nb_read_frames,nb_frames",
        "-of",
        "json",
        str(video_path),
        ]
    )
    completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout_s)
    data: dict[str, Any] = json.loads(completed.stdout)
    streams = data.get("streams") or []
    if not streams:
        return None
    value = streams[0].get("nb_read_frames") or streams[0].get("nb_frames")
    return int(value) if value is not None else None


@dataclass(frozen=True)
class CodecLosslessReport:
    profile_id: str
    frames: int
    width: int
    height: int
    fps: float
    encoded_path: str
    encoded_bytes: int
    source_hash_count: int
    decoded_hash_count: int
    hashes_match: bool
    first_mismatch_index: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CodecBenchmarkResult:
    name: str
    codec: str
    container: str
    frames: int
    width: int
    height: int
    fps: float
    output_path: str
    output_bytes: int
    encode_seconds: float
    encode_fps: float
    realtime_factor: float
    approx_mbps: float
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SessionPixelVerificationReport:
    session_dir: str
    status: str
    profile_id: str
    pixel_fidelity: str
    source_pixel_format: str
    channel_semantics: str
    decoded_mode: str
    expected_frames: int | None
    frame_rows: int
    segment_rows: int
    segment_frame_count_sum: int
    ffprobe_frames_total: int | None
    decoded_hash_count: int
    decode_limited: bool
    exact_source_hashes_available: bool
    source_hash_count: int
    source_hash_compared_count: int
    source_hash_every: int
    hashes_match: bool | None
    first_mismatch_index: int | None
    report_qc_status: str
    acquisition_issues: list[str]
    issues: list[str]
    warnings: list[str]
    recommendation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_lossless_codec(
    cfg: PyCamRecConfig,
    output_root: Path,
    frames: int = 100,
) -> CodecLosslessReport:
    if frames <= 0:
        raise ValueError("frames must be positive.")
    output_root.mkdir(parents=True, exist_ok=True)
    test_dir = output_root / "codec_tests" / f"{int(time.time())}_{cfg.recording_profile.id}"
    test_dir.mkdir(parents=True, exist_ok=False)

    source_md5 = test_dir / "source.framemd5"
    decoded_md5 = test_dir / "decoded.framemd5"
    encoded_path = test_dir / f"encoded.{cfg.writer.container}"
    source_filter = (
        f"testsrc2=size={cfg.camera.expected_width}x{cfg.camera.expected_height}:"
        f"rate={_format_fps(cfg.camera.expected_fps)},format=gray"
    )

    _run_ffmpeg(
        [
            cfg.writer.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-f",
            "lavfi",
            "-i",
            source_filter,
            "-frames:v",
            str(frames),
            "-f",
            "framemd5",
            str(source_md5),
        ]
    )
    _run_ffmpeg(
        [
            cfg.writer.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-f",
            "lavfi",
            "-i",
            source_filter,
            "-frames:v",
            str(frames),
            "-an",
            *cfg.writer.output_args,
            str(encoded_path),
        ]
    )
    _run_ffmpeg(
        [
            cfg.writer.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            str(encoded_path),
            "-frames:v",
            str(frames),
            "-vf",
            "format=gray",
            "-f",
            "framemd5",
            str(decoded_md5),
        ]
    )

    source_hashes = _read_framemd5_hashes(source_md5)
    decoded_hashes = _read_framemd5_hashes(decoded_md5)
    first_mismatch = _first_mismatch(source_hashes, decoded_hashes)
    return CodecLosslessReport(
        profile_id=cfg.recording_profile.id,
        frames=frames,
        width=cfg.camera.expected_width,
        height=cfg.camera.expected_height,
        fps=cfg.camera.expected_fps,
        encoded_path=str(encoded_path),
        encoded_bytes=encoded_path.stat().st_size,
        source_hash_count=len(source_hashes),
        decoded_hash_count=len(decoded_hashes),
        hashes_match=first_mismatch is None and len(source_hashes) == len(decoded_hashes),
        first_mismatch_index=first_mismatch,
    )


def verify_session_pixels(
    session_dir: Path,
    *,
    max_decode_frames: int | None = None,
    timeout_s: float = 3600.0,
) -> SessionPixelVerificationReport:
    """Verify recorded session video decode/count integrity.

    Existing sessions do not contain original source-frame hashes by default, so this
    proves that the saved videos decode as the expected gray stream and that frame
    counts agree. Exact source-vs-decoded pixel proof is reported only when future
    sessions include a compatible source hash column in frames.csv.
    """
    if max_decode_frames is not None and max_decode_frames <= 0:
        raise ValueError("max_decode_frames must be positive when provided.")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive.")

    session_dir = session_dir.expanduser().resolve()
    session_path = session_dir / "session.json"
    segments_path = session_dir / "segments.csv"
    frames_path = session_dir / "frames.csv"
    if not session_path.is_file():
        raise FileNotFoundError(f"session.json not found: {session_path}")
    if not segments_path.is_file():
        raise FileNotFoundError(f"segments.csv not found: {segments_path}")
    if not frames_path.is_file():
        raise FileNotFoundError(f"frames.csv not found: {frames_path}")

    session = json.loads(session_path.read_text(encoding="utf-8"))
    report = build_session_report(session_dir)
    frames = _read_csv_rows(frames_path)
    segments = _read_csv_rows(segments_path)
    resolved = session.get("resolved") if isinstance(session.get("resolved"), dict) else {}
    writer = resolved.get("writer") if isinstance(resolved.get("writer"), dict) else {}
    camera = resolved.get("camera") if isinstance(resolved.get("camera"), dict) else {}
    metadata_cfg = resolved.get("metadata") if isinstance(resolved.get("metadata"), dict) else {}
    profile = resolved.get("recording_profile") if isinstance(resolved.get("recording_profile"), dict) else {}
    if not profile:
        profile = report.get("recording_profile") if isinstance(report.get("recording_profile"), dict) else {}

    ffmpeg_path = str(writer.get("ffmpeg_path") or _nested(session, ["config", "writer", "ffmpeg_path"]) or "ffmpeg")
    ffprobe_path = str(writer.get("ffprobe_path") or _nested(session, ["config", "writer", "ffprobe_path"]) or "ffprobe")
    source_pixel_format = str(
        camera.get("expected_pixel_format")
        or _nested(session, ["device_info", "pixel_format"])
        or _nested(session, ["config", "camera", "expected_pixel_format"])
        or ""
    )
    semantics = channel_semantics(source_pixel_format) if source_pixel_format else ""
    profile_id = str(profile.get("id") or "")
    pixel_fidelity = str(profile.get("pixel_fidelity") or "")

    acquisition_issues: list[str] = []
    issues: list[str] = []
    warnings: list[str] = []
    segment_frame_sum = sum(_to_int(row.get("frame_count")) or 0 for row in segments)
    expected_frames = _to_int((report.get("frames") or {}).get("expected"))
    if expected_frames is not None and len(frames) != expected_frames:
        acquisition_issues.append(f"frames.csv row count {len(frames)} does not match expected {expected_frames}.")
    if segment_frame_sum != len(frames):
        acquisition_issues.append(
            f"segments.csv frame total {segment_frame_sum} does not match frames.csv row count {len(frames)}."
        )

    source_hash_column = _source_hash_column(frames)
    source_hash_pairs = _source_hash_pairs(frames, source_hash_column)
    exact_hashes_available = bool(source_hash_pairs)
    requested_global_indices = (
        set(range(segment_frame_sum))
        if max_decode_frames is None
        else set(_evenly_spaced_indices(segment_frame_sum, max_decode_frames))
    )
    if exact_hashes_available:
        requested_global_indices.update(frame_index for frame_index, _ in source_hash_pairs)

    ffprobe_total = 0
    ffprobe_known = True
    decoded_hashes_by_index: dict[int, str] = {}
    decode_limited = len(requested_global_indices) < segment_frame_sum
    for row in segments:
        segment_path = _segment_path(row, session_dir)
        if not segment_path.is_file():
            issues.append(f"segment file not found: {segment_path}")
            continue
        try:
            probed = ffprobe_video_frames(
                ffprobe_path,
                segment_path,
                timeout_s=timeout_s,
                count_frames=max_decode_frames is None,
            )
        except subprocess.SubprocessError as exc:
            probed = None
            warnings.append(f"ffprobe frame count failed for {segment_path.name}: {exc!r}")
        if probed is None:
            ffprobe_known = False
        else:
            ffprobe_total += probed
        first_frame_index = _to_int(row.get("first_frame_index")) or 0
        segment_frame_count = _to_int(row.get("frame_count")) or 0
        frame_offsets = (
            None
            if max_decode_frames is None
            else sorted(
                frame_index - first_frame_index
                for frame_index in requested_global_indices
                if first_frame_index <= frame_index < first_frame_index + segment_frame_count
            )
        )
        segment_hashes = _ffmpeg_gray_framemd5_hashes(
            ffmpeg_path,
            segment_path,
            max_frames=None,
            frame_offsets=frame_offsets,
            timeout_s=timeout_s,
        )
        decoded_offsets = frame_offsets if frame_offsets is not None else list(range(len(segment_hashes)))
        for offset, frame_hash in zip(decoded_offsets, segment_hashes):
            decoded_hashes_by_index[first_frame_index + offset] = frame_hash

    ffprobe_total_value = ffprobe_total if ffprobe_known else None
    if ffprobe_total_value is not None and ffprobe_total_value != segment_frame_sum:
        message = f"ffprobe frame total {ffprobe_total_value} does not match segments.csv frame total {segment_frame_sum}."
        if decode_limited:
            warnings.append(message + " Limited pixel verification did not force a full packet count.")
        else:
            acquisition_issues.append(message)
    expected_decoded = len(requested_global_indices)
    if decoded_hashes_by_index and len(decoded_hashes_by_index) != expected_decoded:
        issues.append(
            f"decoded frame hash count {len(decoded_hashes_by_index)} does not match expected decoded count {expected_decoded}."
        )

    source_hash_every = _to_int(metadata_cfg.get("source_frame_hash_every")) or 0
    hashes_match: bool | None = None
    first_mismatch: int | None = None
    compared_hashes = 0
    source_hashes_compatible = semantics not in {"rgb", "bgr"}
    if exact_hashes_available and not source_hashes_compatible:
        issues.append(
            "RGB/BGR source hashes cannot be compared to the current gray/luma decode verifier; "
            "exact color pixel fidelity is not verified for this session."
        )
    elif exact_hashes_available:
        comparable_pairs = [
            (frame_index, source_hash)
            for frame_index, source_hash in source_hash_pairs
            if frame_index in decoded_hashes_by_index
        ]
        compared_hashes = len(comparable_pairs)
        for frame_index, source_hash in comparable_pairs:
            if source_hash != decoded_hashes_by_index[frame_index]:
                first_mismatch = frame_index
                break
        hashes_match = first_mismatch is None and compared_hashes > 0
        if compared_hashes == 0:
            issues.append("source frame hashes are present, but none were inside the decoded frame range.")
        if not hashes_match:
            issues.append("decoded gray frame hashes do not match source frame hashes.")
    else:
        warnings.append(
            "No source-frame hash column is present in frames.csv; exact source-vs-decoded camera pixel equality "
            "cannot be proven retrospectively for this session."
        )
    if semantics == "mono_luma":
        warnings.append("Mono8 video may open as RGB/BGR with identical channels in ImageJ/OpenCV; use gray/luma for analysis.")
    elif semantics.startswith("raw_bayer"):
        warnings.append("Raw Bayer video decodes as a single-channel mosaic; debayer offline for color analysis.")

    qc_status = str((report.get("qc") or {}).get("status") or "")
    if qc_status and qc_status != "pass":
        acquisition_issues.append(f"session report QC status is {qc_status!r}, not 'pass'.")
        warnings.append(
            f"Acquisition QC is {qc_status!r}; pixel fidelity is reported independently below."
        )
    if issues:
        status = "fail"
    elif exact_hashes_available and hashes_match:
        status = (
            "pass_exact_pixels"
            if not decode_limited and _hashes_cover_full_decoded_range(source_hash_pairs, decoded_hashes_by_index)
            else "pass_exact_pixel_sample"
        )
    elif decode_limited:
        status = "pass_decode_sample"
    else:
        status = "pass_decode_integrity"
    result = SessionPixelVerificationReport(
        session_dir=str(session_dir),
        status=status,
        profile_id=profile_id,
        pixel_fidelity=pixel_fidelity,
        source_pixel_format=source_pixel_format,
        channel_semantics=semantics,
        decoded_mode="gray",
        expected_frames=expected_frames,
        frame_rows=len(frames),
        segment_rows=len(segments),
        segment_frame_count_sum=segment_frame_sum,
        ffprobe_frames_total=ffprobe_total_value,
        decoded_hash_count=len(decoded_hashes_by_index),
        decode_limited=decode_limited,
        exact_source_hashes_available=exact_hashes_available,
        source_hash_count=len(source_hash_pairs),
        source_hash_compared_count=compared_hashes,
        source_hash_every=source_hash_every,
        hashes_match=hashes_match,
        first_mismatch_index=first_mismatch,
        report_qc_status=qc_status,
        acquisition_issues=acquisition_issues,
        issues=issues,
        warnings=warnings,
        recommendation=_session_pixel_recommendation(status, pixel_fidelity, exact_hashes_available, semantics),
    )
    (session_dir / "pixel_verification.json").write_text(
        json.dumps(result.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return result


def benchmark_codecs(
    cfg: PyCamRecConfig,
    output_root: Path,
    frames: int = 300,
    include_slow: bool = False,
) -> list[CodecBenchmarkResult]:
    if frames <= 0:
        raise ValueError("frames must be positive.")
    output_root.mkdir(parents=True, exist_ok=True)
    test_dir = output_root / "codec_benchmarks" / f"{int(time.time())}_{cfg.camera.make}_{cfg.camera.serial}"
    test_dir.mkdir(parents=True, exist_ok=False)
    source_filter = (
        f"testsrc2=size={cfg.camera.expected_width}x{cfg.camera.expected_height}:"
        f"rate={_format_fps(cfg.camera.expected_fps)},format=gray"
    )

    results: list[CodecBenchmarkResult] = []
    for preset in _benchmark_presets(include_slow=include_slow):
        output_path = test_dir / f"{preset['name']}.{preset['container']}"
        command = [
            cfg.writer.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-f",
            "lavfi",
            "-i",
            source_filter,
            "-frames:v",
            str(frames),
            "-an",
            *preset["args"],
            str(output_path),
        ]
        started = time.perf_counter()
        error = ""
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=1800)
        except subprocess.CalledProcessError as exc:
            error = (exc.stderr or exc.stdout or repr(exc)).strip()
        except Exception as exc:
            error = repr(exc)
        elapsed = max(time.perf_counter() - started, 1e-9)
        output_bytes = output_path.stat().st_size if output_path.exists() else 0
        encode_fps = frames / elapsed
        results.append(
            CodecBenchmarkResult(
                name=str(preset["name"]),
                codec=str(preset["codec"]),
                container=str(preset["container"]),
                frames=frames,
                width=cfg.camera.expected_width,
                height=cfg.camera.expected_height,
                fps=cfg.camera.expected_fps,
                output_path=str(output_path),
                output_bytes=output_bytes,
                encode_seconds=elapsed,
                encode_fps=encode_fps,
                realtime_factor=encode_fps / cfg.camera.expected_fps,
                approx_mbps=(output_bytes * 8 * cfg.camera.expected_fps / frames / 1_000_000),
                error=error[:2000],
            )
        )
    return results


def _benchmark_presets(include_slow: bool = False) -> list[dict[str, Any]]:
    presets: list[dict[str, Any]] = [
        {
            "name": "raw_gray",
            "codec": "rawvideo",
            "container": "avi",
            "args": ["-c:v", "rawvideo", "-pix_fmt", "gray"],
        },
        {
            "name": "ffv1_gray",
            "codec": "ffv1",
            "container": "mkv",
            "args": ["-c:v", "ffv1", "-level", "3", "-g", "1", "-slicecrc", "1", "-pix_fmt", "gray"],
        },
        _nvenc_bitrate_preset("h264_nvenc_100m", "h264_nvenc", "100M"),
        _nvenc_bitrate_preset("h264_nvenc_200m", "h264_nvenc", "200M"),
        _nvenc_bitrate_preset("h264_nvenc_400m", "h264_nvenc", "400M"),
        _nvenc_bitrate_preset("hevc_nvenc_200m", "hevc_nvenc", "200M"),
        _nvenc_bitrate_preset("hevc_nvenc_400m", "hevc_nvenc", "400M"),
    ]
    if include_slow:
        presets.extend(
            [
                {
                    "name": "libx265_lossless_ultrafast",
                    "codec": "libx265",
                    "container": "mkv",
                    "args": [
                        "-c:v",
                        "libx265",
                        "-preset",
                        "ultrafast",
                        "-x265-params",
                        "lossless=1",
                        "-pix_fmt",
                        "yuv420p",
                    ],
                },
                {
                    "name": "libsvtav1_realtime_crf30",
                    "codec": "libsvtav1",
                    "container": "mkv",
                    "args": ["-c:v", "libsvtav1", "-preset", "12", "-crf", "30", "-pix_fmt", "yuv420p"],
                },
                {
                    "name": "prores_ks_4444",
                    "codec": "prores_ks",
                    "container": "mov",
                    "args": ["-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuva444p10le"],
                },
            ]
        )
    return presets


def _nvenc_bitrate_preset(name: str, codec: str, bitrate: str) -> dict[str, Any]:
    return {
        "name": name,
        "codec": codec,
        "container": "mkv",
        "args": [
            "-c:v",
            codec,
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
            bitrate,
            "-bf",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "pc",
        ],
    }


def _read_framemd5_hashes(path: Path) -> list[str]:
    hashes = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line or not line[0].isdigit():
                continue
            hashes.append(line.rsplit(",", maxsplit=1)[-1].strip())
    return hashes


def _read_framemd5_hashes_from_text(text: str) -> list[str]:
    hashes = []
    for line in text.splitlines():
        if not line or not line[0].isdigit():
            continue
        hashes.append(line.rsplit(",", maxsplit=1)[-1].strip())
    return hashes


def _first_mismatch(left: list[str], right: list[str]) -> int | None:
    for index, (left_hash, right_hash) in enumerate(zip(left, right)):
        if left_hash != right_hash:
            return index
    if len(left) != len(right):
        return min(len(left), len(right))
    return None


def _hashes_cover_full_decoded_range(
    source_hash_pairs: list[tuple[int, str]],
    decoded_hashes_by_index: dict[int, str],
) -> bool:
    if not decoded_hashes_by_index:
        return False
    source_indices = {frame_index for frame_index, _ in source_hash_pairs}
    return all(frame_index in source_indices for frame_index in decoded_hashes_by_index)


def _run_ffmpeg(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=300)


def _ffmpeg_gray_framemd5_hashes(
    ffmpeg_path: str,
    video_path: Path,
    *,
    max_frames: int | None,
    frame_offsets: list[int] | None = None,
    timeout_s: float,
) -> list[str]:
    if frame_offsets == []:
        return []
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(video_path),
        "-map",
        "0:v:0",
    ]
    if max_frames is not None:
        command.extend(["-frames:v", str(max_frames)])
    video_filter = "format=gray"
    if frame_offsets is not None:
        selector = "+".join(f"eq(n\\,{offset})" for offset in frame_offsets)
        video_filter = f"select={selector},format=gray"
        command.extend(["-vsync", "0"])
    command.extend(["-vf", video_filter, "-f", "framemd5", "-"])
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    return _read_framemd5_hashes_from_text(completed.stdout)


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _segment_path(row: dict[str, str], session_dir: Path) -> Path:
    path = Path(str(row.get("path") or ""))
    if path.is_absolute():
        return path
    return session_dir / path


def _source_hash_column(rows: list[dict[str, str]]) -> str | None:
    if not rows:
        return None
    candidates = ("source_framemd5", "raw_framemd5", "frame_framemd5", "source_md5", "raw_md5", "frame_md5")
    keys = set(rows[0])
    for candidate in candidates:
        if candidate in keys:
            return candidate
    return None


def _source_hash_pairs(rows: list[dict[str, str]], column: str | None) -> list[tuple[int, str]]:
    if not column:
        return []
    pairs: list[tuple[int, str]] = []
    for row in rows:
        frame_hash = str(row.get(column) or "").strip()
        if not frame_hash:
            continue
        frame_index = _to_int(row.get("frame_index"))
        if frame_index is None:
            continue
        pairs.append((frame_index, frame_hash))
    return pairs


def _evenly_spaced_indices(total_frames: int, sample_count: int) -> list[int]:
    if total_frames <= 0 or sample_count <= 0:
        return []
    if sample_count >= total_frames:
        return list(range(total_frames))
    if sample_count == 1:
        return [0]
    last = total_frames - 1
    return sorted({round(index * last / (sample_count - 1)) for index in range(sample_count)})


def _nested(data: dict[str, Any], path: list[str]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _session_pixel_recommendation(
    status: str,
    pixel_fidelity: str,
    exact_hashes_available: bool,
    semantics: str,
) -> str:
    if status == "fail":
        return "Do not use this session as scientific source data until the listed issues are resolved."
    if exact_hashes_available and status == "pass_exact_pixels":
        return "Decoded gray frames exactly match recorded source hashes for every decoded frame."
    if exact_hashes_available and status == "pass_exact_pixel_sample":
        return "Decoded gray frames match the recorded source hashes for the sampled/hash-enabled frame range."
    if pixel_fidelity == "lossless":
        return (
            "Session decode/count integrity passed for a lossless profile. Existing metadata does not contain "
            "source-frame hashes, so exact camera-byte equality is inferred from the lossless codec configuration "
            "and synthetic verify-codec results, not proven from this session alone."
        )
    if semantics.startswith("raw_bayer"):
        return "Decoded frames are raw Bayer mosaic bytes; debayer a copy for color review while preserving this source."
    return "Session decode/count integrity passed. Use this as analysis-compatible video, with pixel fidelity as declared by the profile."


def _format_fps(fps: float) -> str:
    if abs(fps - round(fps)) < 1e-9:
        return str(int(round(fps)))
    return f"{fps:.6f}".rstrip("0").rstrip(".")
