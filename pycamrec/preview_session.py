"""Camera live-view session without recording."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .basler_device import BaslerCamera
from .preview import PreviewWorker
from .schemas import PyCamRecConfig


@dataclass
class PreviewSessionStats:
    frames_grabbed: int = 0
    preview_enabled: bool = False
    preview_frames_published: int = 0
    preview_frames_displayed: int = 0
    preview_frames_dropped: int = 0
    preview_error: str = ""
    started_perf_counter_ns: int = 0
    finished_perf_counter_ns: int = 0
    last_camera_temperature_c: float | None = None
    error: str = ""


class PreviewSession:
    """Run camera acquisition only for live view.

    This deliberately avoids the recorder/writer path. The camera is opened,
    frames are sampled into the configured preview sink, then the camera is
    released before scientific recording starts.
    """

    def __init__(
        self,
        cfg: PyCamRecConfig,
        *,
        stop_file: str | Path | None = None,
        max_seconds: float | None = None,
    ):
        self.cfg = cfg
        self.stop_file = Path(stop_file) if stop_file is not None else None
        self.max_seconds = max_seconds
        self.stats = PreviewSessionStats()
        self._stop_file_seen = False
        self._preview: PreviewWorker | None = None

    def run(self) -> PreviewSessionStats:
        if not self.cfg.preview.enabled:
            raise ValueError("PreviewSession requires preview.enabled=true.")

        with BaslerCamera(self.cfg.camera) as camera:
            self.stats.last_camera_temperature_c = _safe_round_float(
                camera.device_info.get("device_temperature_c"),
            )
            self._preview = PreviewWorker(
                self.cfg.preview,
                source_width=self.cfg.camera.expected_width,
                source_height=self.cfg.camera.expected_height,
                source_fps=self.cfg.camera.expected_fps,
                queue_max_frames=self.cfg.writer.queue_max_frames,
                session_dir=self.cfg.session.output_root,
                recording_profile_id=self.cfg.recording_profile.id,
                metrics_provider=self._preview_metrics,
            )
            if not self._preview.start():
                self.stats.preview_error = self._preview.error if self._preview is not None else ""
                return self.stats

            self.stats.preview_enabled = True
            self.stats.started_perf_counter_ns = time.perf_counter_ns()
            started = time.perf_counter()
            camera.start()
            try:
                while True:
                    if self._external_stop_requested():
                        break
                    if self.max_seconds is not None and time.perf_counter() - started >= self.max_seconds:
                        break
                    frame = camera.grab_frame()
                    if frame is None:
                        continue
                    frame_index = self.stats.frames_grabbed
                    self.stats.frames_grabbed += 1
                    self._preview.publish(
                        frame_index=frame_index,
                        frame_bytes=frame.frame_bytes,
                        writer_segment_id=0,
                        queue_depth=0,
                        dropped_detected=0,
                    )
            except BaseException as exc:
                self.stats.error = repr(exc)
                raise
            finally:
                camera.stop()
                self._stop_preview()
                camera.close()
                self.stats.finished_perf_counter_ns = time.perf_counter_ns()
        return self.stats

    def _preview_metrics(self) -> dict[str, object]:
        elapsed_s = 0.0
        if self.stats.started_perf_counter_ns:
            elapsed_s = (time.perf_counter_ns() - self.stats.started_perf_counter_ns) / 1_000_000_000.0
        return {
            "status": "PREVIEW",
            "elapsed_s": elapsed_s,
            "frames_grabbed": self.stats.frames_grabbed,
            "frames_written": 0,
            "expected_frames": 0,
            "free_space_gb": None,
            "camera_temperature_c": self.stats.last_camera_temperature_c,
            "dropped_detected": 0,
        }

    def _stop_preview(self) -> None:
        if self._preview is None:
            return
        self._preview.stop()
        self._preview.join()
        summary = self._preview.summary()
        self.stats.preview_enabled = bool(summary["enabled"])
        self.stats.preview_frames_published = int(summary["frames_published"])
        self.stats.preview_frames_displayed = int(summary["frames_displayed"])
        self.stats.preview_frames_dropped = int(summary["frames_dropped"])
        self.stats.preview_error = str(summary["error"])

    def _external_stop_requested(self) -> bool:
        if self.stop_file is None or self._stop_file_seen:
            return self._stop_file_seen
        try:
            self._stop_file_seen = self.stop_file.exists()
        except OSError:
            self._stop_file_seen = False
        return self._stop_file_seen


def stats_to_dict(stats: PreviewSessionStats) -> dict[str, object]:
    return asdict(stats)


def _safe_round_float(value: object, digits: int = 3) -> float | None:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None
