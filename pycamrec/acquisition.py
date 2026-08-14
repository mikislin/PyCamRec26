"""Single-camera acquisition orchestration."""

from __future__ import annotations

import hashlib
import queue
import shutil
import threading
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

from .camera_backend import CameraBackend, GrabbedFrame, create_camera_backend
from .metadata import FrameMetadata, MetadataWriter
from .hardware import build_hardware_fingerprint, build_profile_fingerprint, evaluate_profile_approval
from .preflight import PreflightReport
from .preview import PreviewWorker
from .schemas import PyCamRecConfig
from .writer_ffmpeg import FfmpegSegmentWriter


@dataclass(frozen=True)
class FramePacket:
    frame_index: int
    frame: GrabbedFrame
    dropped_before_frame: int
    queue_depth_after_enqueue: int
    writer_segment_id: int


@dataclass
class RecordingStats:
    expected_frames: int = 0
    frames_grabbed: int = 0
    frames_written: int = 0
    frames_with_block_id: int = 0
    frames_with_camera_timestamp: int = 0
    dropped_detected: int = 0
    grab_timeouts: int = 0
    queue_capacity_frames: int = 0
    max_queue_depth: int = 0
    queue_pressure_events: int = 0
    queue_full_errors: int = 0
    health_checks: int = 0
    health_warnings: int = 0
    health_critical_events: int = 0
    last_free_space_gb: float | None = None
    last_camera_temperature_c: float | None = None
    writer_spool_enabled: bool = False
    writer_spool_total_bytes: int = 0
    writer_spool_max_queued_bytes: int = 0
    writer_spool_limit_bytes: int = 0
    preview_enabled: bool = False
    preview_frames_published: int = 0
    preview_frames_displayed: int = 0
    preview_frames_dropped: int = 0
    preview_frames_shed_queue: int = 0
    preview_frames_shed_fps: int = 0
    preview_error: str = ""
    started_perf_counter_ns: int = 0
    finished_perf_counter_ns: int = 0
    error: str = ""


class Recorder:
    def __init__(
        self,
        cfg: PyCamRecConfig,
        preflight: PreflightReport,
        stop_file: str | Path | None = None,
    ):
        self.cfg = cfg
        self.preflight = preflight
        self.stop_file = Path(stop_file) if stop_file is not None else None
        self.stop_event = threading.Event()
        self.frame_queue: queue.Queue[FramePacket] = queue.Queue(maxsize=cfg.writer.queue_max_frames)
        self.stats = RecordingStats()
        self.stats.expected_frames = cfg.expected_total_frames
        self.stats.queue_capacity_frames = cfg.writer.queue_max_frames
        self._producer_error: BaseException | None = None
        self._interrupted = False
        self._preview: PreviewWorker | None = None
        self._stop_file_seen = False
        self._last_progress_print_at = 0.0
        self._progress_stop_event = threading.Event()
        self._last_health_check_at = 0.0

    def run(self) -> RecordingStats:
        with create_camera_backend(self.cfg.camera) as camera:
            evidence_fingerprint = build_hardware_fingerprint(
                camera_config=asdict(self.cfg.camera),
                device_info=camera.device_info,
                pfs_sha256=self.preflight.pfs_sha256,
                ffmpeg_path=self.cfg.writer.ffmpeg_path,
                ffprobe_path=self.cfg.writer.ffprobe_path,
                ffmpeg_version=self.preflight.ffmpeg_version,
            )
            profile_fingerprint = build_profile_fingerprint(
                camera_config=asdict(self.cfg.camera),
                writer_config=asdict(self.cfg.writer),
                recording_profile=asdict(self.cfg.recording_profile),
                preview_config=asdict(self.cfg.preview),
            )
            profile_approval = evaluate_profile_approval(
                self.cfg.raw.get("approval") if isinstance(self.cfg.raw, dict) else {},
                evidence_fingerprint_sha256=str(evidence_fingerprint.get("fingerprint_sha256") or ""),
                profile_fingerprint_sha256=str(profile_fingerprint.get("fingerprint_sha256") or ""),
                preview_enabled=self.cfg.preview.enabled,
                duration_s=self.cfg.session.duration_s,
            )
            if profile_approval["locked"] and not profile_approval["approved"]:
                raise RuntimeError(
                    "Configured profile lock is invalid for this run: "
                    + "; ".join(str(reason) for reason in profile_approval["reasons"])
                    + ". Change approval.status back to requires_hardware_validation and revalidate."
                )
            metadata = MetadataWriter(
                self.cfg,
                self.preflight,
                camera.device_info,
                evidence_fingerprint=evidence_fingerprint,
                profile_fingerprint=profile_fingerprint,
                profile_approval=profile_approval,
            )
            writer = FfmpegSegmentWriter(self.cfg, metadata.segments_dir)
            self.stats.last_camera_temperature_c = _safe_round_float(
                camera.device_info.get("device_temperature_c"),
            )
            self._preview = PreviewWorker(
                self.cfg.preview,
                source_width=self.cfg.camera.expected_width,
                source_height=self.cfg.camera.expected_height,
                source_fps=self.cfg.camera.expected_fps,
                source_pixel_format=self.cfg.camera.expected_pixel_format,
                queue_max_frames=self.cfg.writer.queue_max_frames,
                session_dir=metadata.session_dir,
                recording_profile_id=self.cfg.recording_profile.id,
                metrics_provider=self._preview_metrics,
            )
            if self._preview.start():
                self.stats.preview_enabled = True
                metadata.log_event("preview_started", self._preview.summary())
            producer = threading.Thread(
                target=self._produce_frames,
                args=(camera, metadata),
                daemon=True,
                name="pycamrec-grabber",
            )
            self.stats.started_perf_counter_ns = time.perf_counter_ns()
            camera.start()
            producer.start()
            self._progress_stop_event.clear()
            progress = threading.Thread(
                target=self._run_progress_telemetry,
                daemon=True,
                name="pycamrec-progress",
            )
            progress.start()
            self._emit_progress(force=True)
            try:
                self._consume_frames(writer, metadata, producer, camera)
            except KeyboardInterrupt:
                self._interrupted = True
                print(
                    "[PyCamRec] Interrupt received. Stopping acquisition, draining queued frames, "
                    "and finalizing the current segment...",
                    flush=True,
                )
                self.stop_event.set()
                metadata.log_event("keyboard_interrupt", {})
                self._consume_frames(writer, metadata, producer, camera)
            finally:
                self.stop_event.set()
                producer.join(timeout=10)
                self._progress_stop_event.set()
                progress.join(timeout=2)
                finalization_error: BaseException | None = None
                try:
                    segment = writer.close()
                    if segment is not None:
                        metadata.append_segment(segment)
                        self._record_segment_health(camera, metadata, segment.segment_id, reason="finalization")
                    self._record_writer_spool_stats(writer)
                except Exception as exc:
                    finalization_error = exc
                    self.stats.error = repr(exc)
                    metadata.log_event("finalization_error", {"error": repr(exc)})
                finally:
                    self._record_writer_spool_stats(writer)
                    self._stop_preview(metadata)
                    camera.close()
                    self.stats.finished_perf_counter_ns = time.perf_counter_ns()
                    self._emit_progress(force=True)
                    if self._producer_error and not self.stats.error:
                        self.stats.error = repr(self._producer_error)
                    metadata.close(summary=asdict(self.stats))
                    if finalization_error is None:
                        prefix = "Interrupted session finalized" if self._interrupted else "Finalized"
                        print(
                            f"[PyCamRec] {prefix} {metadata.session_dir}. "
                            "Camera released and metadata closed.",
                            flush=True,
                        )
                    else:
                        print(
                            f"[PyCamRec] Stopped at {metadata.session_dir}. "
                            "Camera released and metadata closed, but final segment close failed.",
                            flush=True,
                        )
                if finalization_error is not None:
                    raise RuntimeError(
                        f"Recording finalization failed: {finalization_error!r}"
                    ) from finalization_error
            if self._producer_error is not None:
                raise RuntimeError(f"Acquisition failed: {self._producer_error!r}") from self._producer_error
            if self.stats.error:
                raise RuntimeError(self.stats.error)
            return self.stats

    def stop(self) -> None:
        self.stop_event.set()

    def _record_writer_spool_stats(self, writer: FfmpegSegmentWriter) -> None:
        summary = writer.spool_summary()
        self.stats.writer_spool_enabled = bool(summary["enabled"])
        self.stats.writer_spool_total_bytes = int(summary["total_bytes"])
        self.stats.writer_spool_max_queued_bytes = int(summary["max_queued_bytes"])
        self.stats.writer_spool_limit_bytes = int(summary["limit_bytes"])

    def _stop_preview(self, metadata: MetadataWriter) -> None:
        if self._preview is None:
            return
        self._preview.stop()
        self._preview.join()
        summary = self._preview.summary()
        self.stats.preview_enabled = bool(summary["enabled"])
        self.stats.preview_frames_published = int(summary["frames_published"])
        self.stats.preview_frames_displayed = int(summary["frames_displayed"])
        self.stats.preview_frames_dropped = int(summary["frames_dropped"])
        self.stats.preview_frames_shed_queue = int(summary["frames_shed_queue"])
        self.stats.preview_frames_shed_fps = int(summary["frames_shed_fps"])
        self.stats.preview_error = str(summary["error"])
        metadata.log_event("preview_summary", summary)

    def _preview_metrics(self) -> dict[str, object]:
        now_ns = time.perf_counter_ns()
        elapsed_s = 0.0
        if self.stats.started_perf_counter_ns:
            elapsed_s = (now_ns - self.stats.started_perf_counter_ns) / 1_000_000_000.0
        status = "STOPPING" if self.stop_event.is_set() else "REC"
        return {
            "status": status,
            "elapsed_s": elapsed_s,
            "frames_grabbed": self.stats.frames_grabbed,
            "frames_written": self.stats.frames_written,
            "expected_frames": self.stats.expected_frames,
            "free_space_gb": self.stats.last_free_space_gb,
            "camera_temperature_c": self.stats.last_camera_temperature_c,
            "dropped_detected": self.stats.dropped_detected,
            "expected_fps": self.cfg.camera.expected_fps,
            "queue_depth": self.frame_queue.qsize(),
            "queue_capacity": self.cfg.writer.queue_max_frames,
        }

    def _publish_preview(self, packet: FramePacket) -> None:
        if self._preview is None:
            return
        self._preview.publish(
            frame_index=packet.frame_index,
            frame_bytes=packet.frame.frame_bytes,
            writer_segment_id=packet.writer_segment_id,
            queue_depth=packet.queue_depth_after_enqueue,
            dropped_detected=self.stats.dropped_detected,
        )

    def _run_progress_telemetry(self) -> None:
        while not self._progress_stop_event.wait(1.0):
            self._emit_progress()

    def _emit_progress(self, force: bool = False) -> None:
        now = time.perf_counter()
        if not force and now - self._last_progress_print_at < 1.0:
            return
        self._last_progress_print_at = now

        elapsed_s = 0.0
        if self.stats.started_perf_counter_ns:
            elapsed_s = (time.perf_counter_ns() - self.stats.started_perf_counter_ns) / 1_000_000_000.0
        fps = self.stats.frames_grabbed / elapsed_s if elapsed_s > 0 else 0.0
        queue_depth = self.frame_queue.qsize()
        segment_id = self.stats.frames_grabbed // max(1, self.cfg.segment_frame_count)
        print(
            f"[PyCamRec] Progress elapsed_s={elapsed_s:.3f} "
            f"frames={self.stats.frames_grabbed}/{self.stats.expected_frames} "
            f"written={self.stats.frames_written} fps={fps:.1f} "
            f"segment={segment_id} queue={queue_depth}/{self.stats.queue_capacity_frames} "
            f"gaps={self.stats.dropped_detected}",
            flush=True,
        )

    def _produce_frames(self, camera: CameraBackend, metadata: MetadataWriter) -> None:
        previous_block_id: int | None = None
        previous_timestamp_raw: int | None = None
        expected_delta_ns = int(round(1_000_000_000 / self.cfg.camera.expected_fps))
        expected_delta_raw = _expected_timestamp_delta_raw(camera, self.cfg.camera.expected_fps)
        consecutive_timeouts = 0
        try:
            while self.stats.frames_grabbed < self.cfg.expected_total_frames:
                if self.stop_event.is_set():
                    break
                if self._external_stop_requested():
                    self._interrupted = True
                    metadata.log_event(
                        "external_stop_requested",
                        {"stop_file": str(self.stop_file) if self.stop_file else None},
                    )
                    break
                frame = camera.grab_frame()
                if frame is None:
                    self.stats.grab_timeouts += 1
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= 5:
                        raise RuntimeError("Camera produced no frames for five consecutive grab timeouts.")
                    continue
                consecutive_timeouts = 0
                frame_index = self.stats.frames_grabbed
                dropped = _detect_gap(
                    frame,
                    previous_block_id=previous_block_id,
                    previous_timestamp_raw=previous_timestamp_raw,
                    expected_delta_raw=expected_delta_raw,
                    expected_delta_ns=expected_delta_ns,
                )
                previous_block_id = frame.camera_block_id
                previous_timestamp_raw = frame.camera_timestamp_raw
                segment_id = frame_index // self.cfg.segment_frame_count

                queue_depth = min(
                    self.cfg.writer.queue_max_frames,
                    self.frame_queue.qsize() + 1,
                )
                packet = FramePacket(
                    frame_index=frame_index,
                    frame=frame,
                    dropped_before_frame=dropped,
                    queue_depth_after_enqueue=queue_depth,
                    writer_segment_id=segment_id,
                )
                try:
                    self.frame_queue.put(packet, timeout=1.0)
                except queue.Full as exc:
                    self.stats.queue_full_errors += 1
                    self.stop_event.set()
                    raise RuntimeError(
                        "Writer queue filled. Aborting to avoid dropped frames."
                    ) from exc

                queue_depth = max(queue_depth, self.frame_queue.qsize())
                self.stats.max_queue_depth = max(self.stats.max_queue_depth, queue_depth)
                if queue_depth >= int(self.cfg.writer.queue_max_frames * 0.90):
                    self.stats.queue_pressure_events += 1
                self.stats.frames_grabbed += 1
                if frame.camera_block_id is not None:
                    self.stats.frames_with_block_id += 1
                if frame.camera_timestamp_raw is not None or frame.camera_timestamp_ns is not None:
                    self.stats.frames_with_camera_timestamp += 1
                self.stats.dropped_detected += dropped
                self._publish_preview(packet)
                if dropped:
                    metadata.log_event(
                        "frame_gap_detected",
                        {
                            "frame_index": frame_index,
                            "camera_block_id": frame.camera_block_id,
                            "camera_timestamp_raw": frame.camera_timestamp_raw,
                            "camera_timestamp_ns": frame.camera_timestamp_ns,
                            "dropped_estimate": dropped,
                        },
                    )
                    if self.cfg.camera.strict_validation:
                        self.stop_event.set()
                        raise RuntimeError("Frame continuity gap detected.")
        except BaseException as exc:
            self._producer_error = exc
            self.stop_event.set()

    def _external_stop_requested(self) -> bool:
        if self.stop_file is None or self._stop_file_seen:
            return self._stop_file_seen
        try:
            self._stop_file_seen = self.stop_file.exists()
        except OSError:
            self._stop_file_seen = False
        if self._stop_file_seen:
            self.stop_event.set()
        return self._stop_file_seen

    def _consume_frames(
        self,
        writer: FfmpegSegmentWriter,
        metadata: MetadataWriter,
        producer: threading.Thread,
        camera: CameraBackend,
    ) -> None:
        while producer.is_alive() or not self.frame_queue.empty():
            try:
                packet = self.frame_queue.get(timeout=0.1)
            except queue.Empty:
                if self._producer_error is not None:
                    break
                continue
            try:
                metadata.append_frame(_frame_metadata(packet, self.cfg))
                writer.write_frame(packet.frame_index, packet.frame.frame_bytes)
                for segment in writer.pop_completed_segments():
                    metadata.append_segment(segment)
                    self._record_segment_health(camera, metadata, segment.segment_id, reason="rollover")
                self.stats.frames_written += 1
                now = time.perf_counter()
                if now - self._last_health_check_at >= self.cfg.camera.health_check_interval_s:
                    self._record_segment_health(
                        camera,
                        metadata,
                        packet.writer_segment_id,
                        reason="periodic",
                    )
                    self._last_health_check_at = now
            except Exception as exc:
                self.stats.error = repr(exc)
                self.stop_event.set()
                metadata.log_event("writer_error", {"error": repr(exc)})
                raise
            finally:
                self.frame_queue.task_done()

    def _record_segment_health(
        self,
        camera: CameraBackend,
        metadata: MetadataWriter,
        segment_id: int,
        *,
        reason: str,
    ) -> None:
        disk_usage = shutil.disk_usage(metadata.session_dir)
        free_space_gb = disk_usage.free / (1024**3)
        temperature_c = camera.read_temperature_c()
        warnings = []
        critical = []
        if free_space_gb < self.cfg.writer.min_free_space_gb:
            critical.append(
                f"free space {free_space_gb:.1f} GiB below {self.cfg.writer.min_free_space_gb:.1f} GiB"
            )
        if temperature_c is not None and temperature_c > self.cfg.camera.temperature_warning_c:
            warnings.append(
                f"camera temperature {temperature_c:.1f} C above {self.cfg.camera.temperature_warning_c:.1f} C"
            )
        if temperature_c is not None and temperature_c >= self.cfg.camera.temperature_critical_c:
            critical.append(
                f"camera temperature {temperature_c:.1f} C reached critical "
                f"{self.cfg.camera.temperature_critical_c:.1f} C"
            )

        self.stats.health_checks += 1
        self.stats.last_free_space_gb = round(free_space_gb, 3)
        self.stats.last_camera_temperature_c = (
            round(temperature_c, 3) if temperature_c is not None else None
        )
        self.stats.health_warnings += len(warnings)
        self.stats.health_critical_events += len(critical)

        status = "CRITICAL" if critical else "WARNING" if warnings else "OK"
        temperature_text = f"{temperature_c:.1f} C" if temperature_c is not None else "unknown"
        message = (
            f"[PyCamRec] Health {status}: segment={segment_id}, reason={reason}, "
            f"free={free_space_gb:.1f} GiB, camera_temp={temperature_text}"
        )
        if warnings or critical:
            message += " (" + "; ".join(critical + warnings) + ")"
        print(message, flush=True)
        metadata.log_event(
            "segment_health",
            {
                "segment_id": segment_id,
                "reason": reason,
                "status": status.lower(),
                "free_space_bytes": disk_usage.free,
                "free_space_gb": round(free_space_gb, 3),
                "camera_temperature_c": (
                    round(temperature_c, 3) if temperature_c is not None else None
                ),
                "temperature_warning_c": self.cfg.camera.temperature_warning_c,
                "temperature_critical_c": self.cfg.camera.temperature_critical_c,
                "min_free_space_gb": self.cfg.writer.min_free_space_gb,
                "warnings": warnings,
                "critical": critical,
            },
        )
        if critical:
            metadata.log_event(
                "health_critical_stop_requested",
                {"segment_id": segment_id, "reason": reason, "critical": critical},
            )
            self.stop_event.set()


def _detect_gap(
    frame: GrabbedFrame,
    previous_block_id: int | None,
    previous_timestamp_raw: int | None,
    expected_delta_raw: int | None,
    expected_delta_ns: int,
) -> int:
    block_gap = 0
    if frame.camera_block_id is not None and previous_block_id is not None:
        delta = frame.camera_block_id - previous_block_id
        if delta > 1:
            block_gap = delta - 1

    timestamp_gap = 0
    if (
        frame.camera_timestamp_raw is not None
        and previous_timestamp_raw is not None
        and expected_delta_raw is not None
    ):
        delta_raw = frame.camera_timestamp_raw - previous_timestamp_raw
        if delta_raw > expected_delta_raw * 1.5:
            timestamp_gap = max(1, round(delta_raw / expected_delta_raw) - 1)

    return max(block_gap, timestamp_gap)


def _expected_timestamp_delta_raw(camera: CameraBackend, expected_fps: float) -> int | None:
    if camera.timestamp_tick_frequency_hz in (None, 0):
        return None
    return int(round(camera.timestamp_tick_frequency_hz / expected_fps))


def _safe_round_float(value: object, digits: int = 3) -> float | None:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _frame_metadata(packet: FramePacket, cfg: PyCamRecConfig) -> FrameMetadata:
    frame = packet.frame
    return FrameMetadata(
        frame_index=packet.frame_index,
        camera_block_id=frame.camera_block_id,
        camera_timestamp_raw=frame.camera_timestamp_raw,
        camera_timestamp_ns=frame.camera_timestamp_ns,
        host_receive_perf_counter_ns=frame.host_receive_perf_counter_ns,
        host_receive_utc_ns=frame.host_receive_utc_ns,
        payload_size_bytes=frame.payload_size_bytes,
        queue_depth_after_enqueue=packet.queue_depth_after_enqueue,
        writer_segment_id=packet.writer_segment_id,
        dropped_before_frame=packet.dropped_before_frame,
        source_framemd5=_source_framemd5(cfg, packet.frame_index, frame.frame_bytes),
    )


def _source_framemd5(cfg: PyCamRecConfig, frame_index: int, frame_bytes: bytes) -> str:
    every = cfg.metadata.source_frame_hash_every
    if frame_index not in _source_hash_frame_indices(
        cfg.expected_total_frames,
        every,
        cfg.metadata.source_frame_hash_max_frames,
    ):
        return ""
    return hashlib.md5(frame_bytes).hexdigest()


@lru_cache(maxsize=128)
def _source_hash_frame_indices(total_frames: int, every: int, max_frames: int) -> frozenset[int]:
    """Return deterministic hash positions spanning the complete planned session."""

    if total_frames <= 0 or every <= 0:
        return frozenset()
    candidates = range(0, total_frames, every)
    candidate_count = (total_frames + every - 1) // every
    if max_frames <= 0 or candidate_count <= max_frames:
        return frozenset(candidates)
    if max_frames == 1:
        return frozenset({0})
    last_candidate = candidate_count - 1
    selected_slots = {
        round(index * last_candidate / (max_frames - 1))
        for index in range(max_frames)
    }
    return frozenset(slot * every for slot in selected_slots)
