"""Segmented FFmpeg writer."""

from __future__ import annotations

import os
import queue
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .metadata import SegmentMetadata
from .preflight import sha256_file
from .schemas import PyCamRecConfig


@dataclass(frozen=True)
class WrittenFrame:
    segment_id: int
    frame_index: int


class FfmpegSegmentWriter:
    def __init__(self, cfg: PyCamRecConfig, segments_dir: Path):
        self.cfg = cfg
        self.segments_dir = segments_dir
        self.segment_id = -1
        self.frames_in_segment = 0
        self.segment_first_frame: int | None = None
        self.segment_started_utc = ""
        self.process: subprocess.Popen[bytes] | None = None
        self.stderr_lines: list[str] = []
        self.stdout_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self._stdout_thread: threading.Thread | None = None
        self._spool_writer_thread: threading.Thread | None = None
        self._spool_queue: queue.Queue[bytes | None] | None = None
        self._spool_errors: list[str] = []
        self._spool_lock = threading.Lock()
        self._spool_current_queued_bytes = 0
        self.spool_total_bytes = 0
        self.spool_max_queued_bytes = 0
        self._completed_segments: list[SegmentMetadata] = []

    def spool_summary(self) -> dict[str, int | bool]:
        return {
            "enabled": self.cfg.writer.spool_output,
            "total_bytes": self.spool_total_bytes,
            "max_queued_bytes": self.spool_max_queued_bytes,
            "limit_bytes": self.cfg.writer.spool_max_bytes if self.cfg.writer.spool_output else 0,
        }

    def write_frame(self, frame_index: int, frame_bytes: bytes) -> WrittenFrame:
        if self.process is None or self.frames_in_segment >= self.cfg.segment_frame_count:
            self._open_next_segment(frame_index)
        assert self.process is not None
        if self.process.stdin is None:
            raise RuntimeError("FFmpeg stdin is not available.")
        if self.process.poll() is not None:
            raise RuntimeError(f"FFmpeg exited early with code {self.process.returncode}.")
        try:
            self.process.stdin.write(frame_bytes)
        except BrokenPipeError as exc:
            tail = "\n".join(self.stderr_lines[-20:])
            raise RuntimeError(
                f"FFmpeg pipe closed while writing segment {self.segment_id}, "
                f"frame {frame_index}. Exit code: {self.process.poll()}.\n{tail}"
            ) from exc
        self.frames_in_segment += 1
        return WrittenFrame(segment_id=self.segment_id, frame_index=frame_index)

    def close(self) -> SegmentMetadata | None:
        return self._close_current_segment()

    def pop_completed_segments(self) -> list[SegmentMetadata]:
        segments = self._completed_segments
        self._completed_segments = []
        return segments

    def _open_next_segment(self, first_frame_index: int) -> None:
        closed_segment = self._close_current_segment()
        if closed_segment is not None:
            self._completed_segments.append(closed_segment)
        self.segment_id += 1
        self.frames_in_segment = 0
        self.segment_first_frame = first_frame_index
        self.segment_started_utc = datetime.now(timezone.utc).isoformat()
        temp_path = self._temp_path(self.segment_id)
        final_path = self._final_path(self.segment_id)
        if final_path.exists() and not self.cfg.writer.overwrite:
            raise FileExistsError(f"Refusing to overwrite existing segment: {final_path}")
        command = self._build_command("pipe:1" if self.cfg.writer.spool_output else temp_path)
        self.stderr_lines = []
        self._spool_errors = []
        self._spool_queue = None
        self._spool_current_queued_bytes = 0
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE if self.cfg.writer.spool_output else subprocess.DEVNULL,
            creationflags=_creationflags(),
        )
        self._stderr_thread = threading.Thread(target=self._drain_stderr, args=(self.process,), daemon=True)
        self._stderr_thread.start()
        if self.cfg.writer.spool_output:
            if self.process.stdout is None:
                raise RuntimeError("FFmpeg stdout is not available for spooled output.")
            max_chunks = max(1, self.cfg.writer.spool_max_bytes // self.cfg.writer.spool_chunk_bytes)
            self._spool_queue = queue.Queue(maxsize=max_chunks)
            self._spool_writer_thread = threading.Thread(
                target=self._write_spooled_stdout_to_disk,
                args=(temp_path, self.process),
                daemon=True,
                name="pycamrec-spool-writer",
            )
            self._stdout_thread = threading.Thread(
                target=self._drain_stdout_to_spool,
                args=(self.process,),
                daemon=True,
                name="pycamrec-ffmpeg-stdout",
            )
            self._spool_writer_thread.start()
            self._stdout_thread.start()

    def _close_current_segment(self) -> SegmentMetadata | None:
        if self.process is None:
            return None
        process = self.process
        self.process = None
        if process.stdin is not None:
            process.stdin.close()
        try:
            exit_code = process.wait(timeout=self.cfg.writer.finalize_timeout_s)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            tail = "\n".join(self.stderr_lines[-20:])
            raise RuntimeError(
                f"FFmpeg segment {self.segment_id} did not finish cleanly before "
                f"{self.cfg.writer.finalize_timeout_s:g}s timeout:\n{tail}"
            ) from exc
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
        if self._stdout_thread is not None:
            self._stdout_thread.join(timeout=1800)
            if self._stdout_thread.is_alive():
                self._spool_errors.append("Timed out draining FFmpeg stdout into RAM spool.")
        if self._spool_writer_thread is not None:
            self._spool_writer_thread.join(timeout=1800)
            if self._spool_writer_thread.is_alive():
                self._spool_errors.append("Timed out writing RAM spool to disk.")
        if process.stderr is not None:
            process.stderr.close()
        if process.stdout is not None:
            process.stdout.close()
        self._stderr_thread = None
        self._stdout_thread = None
        self._spool_writer_thread = None
        if exit_code != 0:
            tail = "\n".join(self.stderr_lines[-20:])
            raise RuntimeError(f"FFmpeg segment {self.segment_id} failed with code {exit_code}:\n{tail}")
        if self._spool_errors:
            raise RuntimeError(
                f"Spooled output failed for FFmpeg segment {self.segment_id}: "
                + "; ".join(self._spool_errors[-5:])
            )
        temp_path = self._temp_path(self.segment_id)
        final_path = self._final_path(self.segment_id)
        temp_path.replace(final_path)
        first = self.segment_first_frame if self.segment_first_frame is not None else 0
        last = first + self.frames_in_segment - 1
        return SegmentMetadata(
            segment_id=self.segment_id,
            path=str(final_path),
            first_frame_index=first,
            last_frame_index=last,
            frame_count=self.frames_in_segment,
            started_utc=self.segment_started_utc,
            finished_utc=datetime.now(timezone.utc).isoformat(),
            size_bytes=final_path.stat().st_size,
            sha256=sha256_file(final_path) if self.cfg.writer.hash_segments else "",
        )

    def _build_command(self, output_path: Path | str) -> list[str]:
        base = [
            self.cfg.writer.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            self.cfg.writer.input_pix_fmt,
            "-s",
            f"{self.cfg.camera.expected_width}x{self.cfg.camera.expected_height}",
            "-r",
            _format_fps(self.cfg.camera.expected_fps),
            "-i",
            "-",
            "-an",
        ]
        if self.cfg.writer.output_args:
            output_args = list(self.cfg.writer.output_args)
        elif self.cfg.writer.mode == "raw_chunked":
            output_args = ["-c:v", "copy"]
        else:
            output_args = [
                "-c:v",
                self.cfg.writer.codec,
                "-preset",
                "p4",
                "-rc",
                "constqp",
                "-qp",
                "18",
                "-bf",
                "0",
                "-pix_fmt",
                self.cfg.writer.output_pix_fmt,
                "-color_range",
                "pc",
            ]
        if self.cfg.writer.spool_output:
            output_args = output_args + ["-f", self.cfg.writer.container]
        return base + output_args + [str(output_path)]

    def _drain_stderr(self, process: subprocess.Popen[bytes]) -> None:
        if process.stderr is None:
            return
        for raw in iter(process.stderr.readline, b""):
            try:
                self.stderr_lines.append(raw.decode("utf-8", errors="replace").rstrip())
            except Exception:
                self.stderr_lines.append(repr(raw))

    def _drain_stdout_to_spool(self, process: subprocess.Popen[bytes]) -> None:
        spool_queue = self._spool_queue
        if process.stdout is None or spool_queue is None:
            return
        try:
            while True:
                chunk = process.stdout.read(self.cfg.writer.spool_chunk_bytes)
                if not chunk:
                    break
                spool_queue.put(chunk)
                self._record_spool_enqueue(len(chunk))
        except BaseException as exc:
            self._spool_errors.append(repr(exc))
        finally:
            while True:
                try:
                    spool_queue.put(None, timeout=30)
                    break
                except queue.Full:
                    if self._spool_writer_thread is not None and not self._spool_writer_thread.is_alive():
                        self._spool_errors.append("Unable to signal spool writer because it stopped early.")
                        break

    def _write_spooled_stdout_to_disk(
        self,
        temp_path: Path,
        process: subprocess.Popen[bytes],
    ) -> None:
        spool_queue = self._spool_queue
        if spool_queue is None:
            return
        try:
            with temp_path.open("wb") as handle:
                while True:
                    chunk = spool_queue.get()
                    try:
                        if chunk is None:
                            return
                        handle.write(chunk)
                        self._record_spool_dequeue(len(chunk))
                    finally:
                        spool_queue.task_done()
        except BaseException as exc:
            self._spool_errors.append(repr(exc))
            if process.poll() is None:
                process.kill()

    def _record_spool_enqueue(self, byte_count: int) -> None:
        with self._spool_lock:
            self.spool_total_bytes += byte_count
            self._spool_current_queued_bytes += byte_count
            self.spool_max_queued_bytes = max(
                self.spool_max_queued_bytes,
                self._spool_current_queued_bytes,
            )

    def _record_spool_dequeue(self, byte_count: int) -> None:
        with self._spool_lock:
            self._spool_current_queued_bytes = max(0, self._spool_current_queued_bytes - byte_count)

    def _temp_path(self, segment_id: int) -> Path:
        return self.segments_dir / f"segment_{segment_id:06d}.part.{self.cfg.writer.container}"

    def _final_path(self, segment_id: int) -> Path:
        return self.segments_dir / f"segment_{segment_id:06d}.{self.cfg.writer.container}"


def _format_fps(fps: float) -> str:
    if abs(fps - round(fps)) < 1e-9:
        return str(int(round(fps)))
    return f"{fps:.6f}".rstrip("0").rstrip(".")


def _creationflags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
