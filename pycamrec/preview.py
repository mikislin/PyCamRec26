"""Best-effort live preview for PyCamRec.

Preview is deliberately isolated from the acquisition and writer hot path. The
recorder publishes only the newest sampled frame. If the preview thread cannot
keep up, older preview frames are overwritten silently.
"""

from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass
import json
from multiprocessing import shared_memory
from pathlib import Path
from typing import Any, Callable

from .schemas import PreviewConfig


MetricsProvider = Callable[[], dict[str, Any]]
SHM_HEADER_BYTES = 4096


@dataclass(frozen=True)
class PreviewFrame:
    frame_index: int
    frame_bytes: bytes
    writer_segment_id: int
    queue_depth: int
    dropped_detected: int


class PreviewWorker:
    def __init__(
        self,
        cfg: PreviewConfig,
        *,
        source_width: int,
        source_height: int,
        source_fps: float,
        queue_max_frames: int,
        session_dir: Path,
        recording_profile_id: str,
        metrics_provider: MetricsProvider,
    ):
        self.cfg = cfg
        self.source_width = source_width
        self.source_height = source_height
        self.source_fps = source_fps
        self.queue_max_frames = queue_max_frames
        self.session_dir = session_dir
        self.recording_profile_id = recording_profile_id
        self.metrics_provider = metrics_provider
        self.sample_every = cfg.sample_every or max(1, int(round(source_fps / cfg.max_fps)))

        self.frames_published = 0
        self.frames_displayed = 0
        self.frames_dropped = 0
        self.error: str = ""

        self._lock = threading.Lock()
        self._frame_ready = threading.Event()
        self._stop_event = threading.Event()
        self._latest_frame: PreviewFrame | None = None
        self._thread: threading.Thread | None = None
        self._free_space_gb: float | None = None
        self._free_space_checked_at = 0.0
        self._pgm_slot_index = 0
        self._pgm_ring_size = 64
        self._numpy: Any | None = None
        self._shm: shared_memory.SharedMemory | None = None
        self._shm_shape: tuple[int, int] | None = None
        self._shm_sequence = 0

    def start(self) -> bool:
        if not self.cfg.enabled:
            return False
        if self.cfg.sink in {"pgm", "shm", "shm_raw"}:
            if self.cfg.image_path is None:
                self.error = f"Preview sink {self.cfg.sink!r} requires preview.image_path."
                print(f"WARNING: {self.error}", flush=True)
                return False
            self.cfg.image_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                import numpy as np

                self._numpy = np
            except ImportError:
                self._numpy = None
            target = self._run_shm if self.cfg.sink in {"shm", "shm_raw"} else self._run_pgm
            self._thread = threading.Thread(target=target, daemon=True, name="pycamrec-preview")
            self._thread.start()
            return True
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
        except ImportError as exc:
            self.error = (
                "Preview requested but OpenCV is not installed. "
                "Recording will continue without preview; install opencv-python to enable it."
            )
            print(f"WARNING: {self.error}", flush=True)
            return False

        self._thread = threading.Thread(target=self._run, daemon=True, name="pycamrec-preview")
        self._thread.start()
        return True

    def publish(
        self,
        *,
        frame_index: int,
        frame_bytes: bytes,
        writer_segment_id: int,
        queue_depth: int,
        dropped_detected: int,
    ) -> None:
        if self._thread is None or self._stop_event.is_set():
            return
        if frame_index % self.sample_every != 0:
            return
        if queue_depth >= int(self.queue_max_frames * 0.90):
            self.frames_dropped += 1
            return
        with self._lock:
            if self._latest_frame is not None:
                self.frames_dropped += 1
            self._latest_frame = PreviewFrame(
                frame_index=frame_index,
                frame_bytes=frame_bytes,
                writer_segment_id=writer_segment_id,
                queue_depth=queue_depth,
                dropped_detected=dropped_detected,
            )
            self.frames_published += 1
            self._frame_ready.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._frame_ready.set()

    def join(self, timeout: float = 5.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.cfg.enabled,
            "sink": self.cfg.sink,
            "running": self._thread is not None and self._thread.is_alive(),
            "sample_every": self.sample_every,
            "target_width": self.cfg.width,
            "target_height": self.cfg.height,
            "target_fps": self.cfg.max_fps,
            "frames_published": self.frames_published,
            "frames_displayed": self.frames_displayed,
            "frames_dropped": self.frames_dropped,
            "error": self.error,
        }

    def _run(self) -> None:
        import cv2
        import numpy as np

        if self.cfg.sink == "file":
            assert self.cfg.image_path is not None
            self.cfg.image_path.parent.mkdir(parents=True, exist_ok=True)

        target_width = self.cfg.width
        target_height = self.cfg.height or max(
            1,
            int(round(self.source_height * target_width / self.source_width)),
        )
        min_display_interval_s = 1.0 / self.cfg.max_fps
        next_display_at = 0.0

        try:
            while not self._stop_event.is_set():
                now = time.perf_counter()
                if now < next_display_at:
                    self._frame_ready.wait(timeout=min(next_display_at - now, 0.05))
                    continue

                frame = self._take_latest()
                if frame is None:
                    self._frame_ready.wait(timeout=0.1)
                    continue

                image = np.frombuffer(frame.frame_bytes, dtype=np.uint8).reshape(
                    self.source_height,
                    self.source_width,
                )
                display = cv2.resize(
                    image,
                    (target_width, target_height),
                    interpolation=cv2.INTER_AREA,
                )
                if self.cfg.overlay:
                    display = self._add_overlay(cv2, display, frame)

                if self.cfg.sink == "file":
                    self._write_preview_file(cv2, display)
                else:
                    cv2.imshow(self.cfg.window_title, display)
                self.frames_displayed += 1
                if self.cfg.sink == "window":
                    key = cv2.waitKey(1) & 0xFF
                    if key in (27, ord("q")):
                        break
                    try:
                        if cv2.getWindowProperty(self.cfg.window_title, cv2.WND_PROP_VISIBLE) < 1:
                            break
                    except Exception:
                        pass
                next_display_at = time.perf_counter() + min_display_interval_s
        except Exception as exc:
            self.error = repr(exc)
            print(f"WARNING: Preview stopped after an error: {self.error}", flush=True)
        finally:
            self._stop_event.set()
            if self.cfg.sink == "window":
                try:
                    cv2.destroyWindow(self.cfg.window_title)
                except Exception:
                    pass

    def _run_pgm(self) -> None:
        min_display_interval_s = 1.0 / self.cfg.max_fps
        next_display_at = 0.0
        assert self.cfg.image_path is not None
        try:
            while not self._stop_event.is_set():
                now = time.perf_counter()
                if now < next_display_at:
                    self._frame_ready.wait(timeout=min(next_display_at - now, 0.05))
                    continue

                frame = self._take_latest()
                if frame is None:
                    self._frame_ready.wait(timeout=0.1)
                    continue

                if self._write_pgm_file(frame):
                    self.frames_displayed += 1
                else:
                    self.frames_dropped += 1
                next_display_at = time.perf_counter() + min_display_interval_s
        except Exception as exc:
            self.error = repr(exc)
            print(f"WARNING: Preview stopped after an error: {self.error}", flush=True)
        finally:
            self._stop_event.set()

    def _run_shm(self) -> None:
        min_display_interval_s = 1.0 / self.cfg.max_fps
        next_display_at = 0.0
        assert self.cfg.image_path is not None
        try:
            while not self._stop_event.is_set():
                now = time.perf_counter()
                if now < next_display_at:
                    self._frame_ready.wait(timeout=min(next_display_at - now, 0.05))
                    continue

                frame = self._take_latest()
                if frame is None:
                    self._frame_ready.wait(timeout=0.1)
                    continue

                if self._write_shm_frame(frame):
                    self.frames_displayed += 1
                else:
                    self.frames_dropped += 1
                next_display_at = time.perf_counter() + min_display_interval_s
        except Exception as exc:
            self.error = repr(exc)
            print(f"WARNING: Preview stopped after an error: {self.error}", flush=True)
        finally:
            self._close_shm()
            self._stop_event.set()

    def _take_latest(self) -> PreviewFrame | None:
        with self._lock:
            frame = self._latest_frame
            self._latest_frame = None
            self._frame_ready.clear()
            return frame

    def _add_overlay(self, cv2: Any, gray_display: Any, frame: PreviewFrame) -> Any:
        display = cv2.cvtColor(gray_display, cv2.COLOR_GRAY2BGR)
        overlay = display.copy()
        cv2.rectangle(overlay, (0, 0), (display.shape[1], 92), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.55, display, 0.45, 0, display)

        metrics = self.metrics_provider()
        elapsed_s = float(metrics.get("elapsed_s") or 0.0)
        frames_grabbed = int(metrics.get("frames_grabbed") or 0)
        expected_frames = int(metrics.get("expected_frames") or 0)
        approx_fps = frames_grabbed / elapsed_s if elapsed_s > 0 else 0.0
        status = str(metrics.get("status") or "REC")
        temp = metrics.get("camera_temperature_c")
        free_gb = self._current_free_space_gb(metrics.get("free_space_gb"))
        queue_text = f"{frame.queue_depth}/{self.queue_max_frames}"
        temp_text = "unknown" if temp is None else f"{float(temp):.1f} C"
        free_text = "unknown" if free_gb is None else f"{free_gb:.1f} GiB"

        lines = [
            f"{status} | {self.recording_profile_id}",
            (
                f"Elapsed {format_duration(elapsed_s)} | Frames {frames_grabbed}/{expected_frames} "
                f"| FPS {approx_fps:.1f}"
            ),
            (
                f"Segment {frame.writer_segment_id} | Queue {queue_text} | Free {free_text} "
                f"| Cam {temp_text} | Gaps {frame.dropped_detected}"
            ),
        ]

        y = 21
        for index, line in enumerate(lines):
            color = (80, 255, 80) if index == 0 and status == "REC" else (245, 245, 245)
            if index == 2 and (frame.dropped_detected or frame.queue_depth >= self.queue_max_frames * 0.9):
                color = (0, 220, 255)
            cv2.putText(
                display,
                line,
                (10, y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                color,
                1,
                cv2.LINE_AA,
            )
            y += 29
        return display

    def _current_free_space_gb(self, fallback: Any) -> float | None:
        now = time.perf_counter()
        if now - self._free_space_checked_at >= 1.0:
            try:
                self._free_space_gb = shutil.disk_usage(self.session_dir).free / (1024**3)
                self._free_space_checked_at = now
            except Exception:
                pass
        if self._free_space_gb is not None:
            return self._free_space_gb
        try:
            return None if fallback is None else float(fallback)
        except (TypeError, ValueError):
            return None

    def _write_preview_file(self, cv2: Any, display: Any) -> None:
        if self.cfg.image_path is None:
            return
        ok, encoded = cv2.imencode(".ppm", display)
        if not ok:
            return
        temp_path = self.cfg.image_path.with_suffix(".tmp")
        temp_path.write_bytes(encoded.tobytes())
        temp_path.replace(self.cfg.image_path)

    def _write_pgm_file(self, frame: PreviewFrame) -> bool:
        if self.cfg.image_path is None:
            return False
        slot_path = self._next_pgm_slot_path()
        temp_path = slot_path.with_name(slot_path.name + ".tmp")

        stride = self._pgm_stride()
        output_width = (self.source_width + stride - 1) // stride
        output_height = (self.source_height + stride - 1) // stride
        header = f"P5\n{output_width} {output_height}\n255\n".encode("ascii")

        try:
            with temp_path.open("wb") as handle:
                handle.write(header)
                handle.write(self._pgm_payload(frame.frame_bytes, stride))
            temp_path.replace(slot_path)
        except OSError:
            try:
                temp_path.unlink()
            except OSError:
                pass
            return False

        metrics = self.metrics_provider()
        elapsed_s = float(metrics.get("elapsed_s") or 0.0)
        frames_grabbed = int(metrics.get("frames_grabbed") or 0)
        approx_fps = frames_grabbed / elapsed_s if elapsed_s > 0 else 0.0
        metadata = {
            "image_path": str(slot_path),
            "frame_index": frame.frame_index,
            "preview_width": output_width,
            "preview_height": output_height,
            "source_width": self.source_width,
            "source_height": self.source_height,
            "downsample_stride": stride,
            "writer_segment_id": frame.writer_segment_id,
            "queue_depth": frame.queue_depth,
            "queue_max_frames": self.queue_max_frames,
            "dropped_detected": frame.dropped_detected,
            "elapsed_s": elapsed_s,
            "frames_grabbed": frames_grabbed,
            "frames_written": int(metrics.get("frames_written") or 0),
            "expected_frames": int(metrics.get("expected_frames") or 0),
            "approx_fps": approx_fps,
            "camera_temperature_c": metrics.get("camera_temperature_c"),
            "free_space_gb": metrics.get("free_space_gb"),
            "recording_profile_id": self.recording_profile_id,
            "status": metrics.get("status") or "REC",
        }
        meta_path = self.cfg.image_path.with_suffix(".json")
        temp_meta_path = self.cfg.image_path.with_suffix(".json.tmp")
        try:
            temp_meta_path.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
            temp_meta_path.replace(meta_path)
        except OSError:
            try:
                temp_meta_path.unlink()
            except OSError:
                pass
            return False
        return True

    def _write_shm_frame(self, frame: PreviewFrame) -> bool:
        if self.cfg.image_path is None:
            return False
        stride = self._pgm_stride()
        output_width = (self.source_width + stride - 1) // stride
        output_height = (self.source_height + stride - 1) // stride
        buffer_width = output_width
        buffer_height = output_height
        buffer_format = "gray8_downsampled"
        try:
            if self.cfg.sink == "shm_raw":
                buffer_width = self.source_width
                buffer_height = self.source_height
                buffer_format = "raw_mono8"
            self._ensure_shm(buffer_width, buffer_height)
            assert self._shm is not None
            if self.cfg.sink == "shm_raw":
                start = SHM_HEADER_BYTES
                end = start + len(frame.frame_bytes)
                self._shm.buf[start:end] = frame.frame_bytes
            elif self._numpy is not None:
                source = self._numpy.frombuffer(frame.frame_bytes, dtype=self._numpy.uint8).reshape(
                    self.source_height,
                    self.source_width,
                )
                target = self._numpy.ndarray(
                    (output_height, output_width),
                    dtype=self._numpy.uint8,
                    buffer=self._shm.buf,
                    offset=SHM_HEADER_BYTES,
                )
                target[:, :] = source[::stride, ::stride]
            else:
                payload = self._pgm_payload(frame.frame_bytes, stride)
                start = SHM_HEADER_BYTES
                self._shm.buf[start : start + len(payload)] = payload
        except Exception:
            return False

        self._shm_sequence += 1
        metrics = self.metrics_provider()
        elapsed_s = float(metrics.get("elapsed_s") or 0.0)
        frames_grabbed = int(metrics.get("frames_grabbed") or 0)
        approx_fps = frames_grabbed / elapsed_s if elapsed_s > 0 else 0.0
        metadata = {
            "sink": "shm",
            "shm_name": self._shm.name if self._shm is not None else "",
            "sequence": self._shm_sequence,
            "preview_width": output_width,
            "preview_height": output_height,
            "buffer_width": buffer_width,
            "buffer_height": buffer_height,
            "buffer_format": buffer_format,
            "source_width": self.source_width,
            "source_height": self.source_height,
            "downsample_stride": stride,
            "frame_index": frame.frame_index,
            "writer_segment_id": frame.writer_segment_id,
            "queue_depth": frame.queue_depth,
            "queue_max_frames": self.queue_max_frames,
            "dropped_detected": frame.dropped_detected,
            "elapsed_s": elapsed_s,
            "frames_grabbed": frames_grabbed,
            "frames_written": int(metrics.get("frames_written") or 0),
            "expected_frames": int(metrics.get("expected_frames") or 0),
            "approx_fps": approx_fps,
            "camera_temperature_c": metrics.get("camera_temperature_c"),
            "free_space_gb": metrics.get("free_space_gb"),
            "recording_profile_id": self.recording_profile_id,
            "status": metrics.get("status") or "REC",
        }
        return self._write_shm_header(metadata)

    def _pgm_stride(self) -> int:
        width_stride = max(1, int(round(self.source_width / max(1, self.cfg.width))))
        if self.cfg.height is None:
            return width_stride
        height_stride = max(1, int(round(self.source_height / max(1, self.cfg.height))))
        return max(width_stride, height_stride)

    def _pgm_payload(self, frame_bytes: bytes, stride: int) -> bytes:
        if stride == 1:
            return frame_bytes
        if self._numpy is not None:
            image = self._numpy.frombuffer(frame_bytes, dtype=self._numpy.uint8).reshape(
                self.source_height,
                self.source_width,
            )
            return image[::stride, ::stride].copy(order="C").tobytes()

        view = memoryview(frame_bytes)
        rows: list[bytes] = []
        for y in range(0, self.source_height, stride):
            start = y * self.source_width
            row = view[start : start + self.source_width]
            rows.append(row[::stride].tobytes())
        return b"".join(rows)

    def _next_pgm_slot_path(self) -> Path:
        assert self.cfg.image_path is not None
        slot = self._pgm_slot_index
        self._pgm_slot_index = (self._pgm_slot_index + 1) % self._pgm_ring_size
        return self.cfg.image_path.with_name(
            f"{self.cfg.image_path.stem}_{slot:02d}{self.cfg.image_path.suffix}"
        )

    def _ensure_shm(self, width: int, height: int) -> None:
        shape = (height, width)
        if self._shm is not None and self._shm_shape == shape:
            return
        self._close_shm()
        size = SHM_HEADER_BYTES + width * height
        base_name = f"pycamrec_preview_{time.time_ns()}"
        for index in range(100):
            name = base_name if index == 0 else f"{base_name}_{index}"
            try:
                self._shm = shared_memory.SharedMemory(name=name, create=True, size=size)
                self._shm_shape = shape
                self._write_shm_sidecar(width, height)
                return
            except FileExistsError:
                continue
        raise RuntimeError("Could not allocate a unique shared-memory preview buffer.")

    def _close_shm(self) -> None:
        if self._shm is None:
            return
        try:
            self._shm.close()
        except Exception:
            pass
        try:
            self._shm.unlink()
        except Exception:
            pass
        self._shm = None
        self._shm_shape = None

    def _write_preview_metadata(self, metadata: dict[str, Any]) -> bool:
        if self.cfg.image_path is None:
            return False
        meta_path = self.cfg.image_path.with_suffix(".json")
        temp_meta_path = self.cfg.image_path.with_suffix(".json.tmp")
        try:
            temp_meta_path.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
            temp_meta_path.replace(meta_path)
        except OSError:
            try:
                temp_meta_path.unlink()
            except OSError:
                pass
            return False
        return True

    def _write_shm_sidecar(self, width: int, height: int) -> bool:
        if self._shm is None:
            return False
        return self._write_preview_metadata(
            {
                "sink": "shm",
                "shm_name": self._shm.name,
                "header_bytes": SHM_HEADER_BYTES,
                "preview_width": width,
                "preview_height": height,
                "source_width": self.source_width,
                "source_height": self.source_height,
                "sequence": 0,
            }
        )

    def _write_shm_header(self, metadata: dict[str, Any]) -> bool:
        if self._shm is None:
            return False
        payload = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(payload) >= SHM_HEADER_BYTES:
            return False
        try:
            self._shm.buf[:SHM_HEADER_BYTES] = payload + b"\0" * (SHM_HEADER_BYTES - len(payload))
        except Exception:
            return False
        return True


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
