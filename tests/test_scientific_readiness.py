from __future__ import annotations

import csv
import json
import shutil
import tempfile
import unittest
import tomllib
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from pycamrec.acquisition import _source_hash_frame_indices
from pycamrec.approval import lock_profile_from_summary
from pycamrec.config import load_config
from pycamrec.gui import _disk_estimate_text, _integrated_preview_sink
from pycamrec.hardware import build_hardware_fingerprint, evaluate_profile_approval
from pycamrec.onboarding import generate_camera_config
from pycamrec.preview import PreviewWorker
from pycamrec.report import build_session_report
from pycamrec.schemas import (
    CameraConfig,
    ExperimentMetadataConfig,
    MetadataConfig,
    PreviewConfig,
    PyCamRecConfig,
    RecordingProfileConfig,
    SessionConfig,
    WriterConfig,
)
from pycamrec.validation_sweep import SweepResult, _profile_lock_recommendation
from pycamrec.verify import _evenly_spaced_indices, _ffmpeg_gray_framemd5_hashes, verify_session_pixels
from pycamrec.writer_ffmpeg import FfmpegSegmentWriter


ROOT = Path(__file__).resolve().parents[1]


class OnboardingTests(unittest.TestCase):
    def _base_config(self, root: Path) -> Path:
        pfs = root / "camera_12345678.pfs"
        pfs.write_text(
            "Width\t2464\nHeight\t2064\nPixelFormat\tMono8\nAcquisitionFrameRate\t200.0\n",
            encoding="utf-8",
        )
        config = root / "base.yaml"
        config.write_text(
            yaml.safe_dump(
                {
                    "profile": "analysis_h264_mp4_250m",
                    "session": {"output_root": str(root / "sessions"), "duration_s": 1},
                    "camera": {
                        "make": "basler_cxp",
                        "serial": "12345678",
                        "pfs_path": str(pfs),
                        "expected_width": 2464,
                        "expected_height": 2064,
                        "expected_pixel_format": "Mono8",
                        "expected_fps": 200,
                    },
                    "writer": {},
                }
            ),
            encoding="utf-8",
        )
        return config

    def test_cxp_onboarding_never_inherits_usb_profile_or_bitrate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = generate_camera_config(
                self._base_config(root),
                output_path=root / "cxp.yaml",
                camera_make="basler_cxp",
                serial="12345678",
                pixel_format="Mono8",
                width=2464,
                height=2064,
                expected_fps=200,
            )
            data = yaml.safe_load(Path(result.path).read_text(encoding="utf-8"))
            self.assertEqual(result.profile, "cxp_mono8_lossless_h264_nvenc_mp4")
            self.assertNotIn("usb", data["session"]["name"].lower())
            self.assertEqual(data["writer"]["expected_bitrate_mbps"], 4000.0)
            self.assertEqual(data["onboarding"]["status"], "generated_unvalidated")

    def test_usb_onboarding_keeps_usb_specific_lossless_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = self._base_config(root)
            usb_pfs = root / "usb_12345678.pfs"
            usb_pfs.write_text(
                "Width\t1280\nHeight\t1024\nPixelFormat\tMono8\nAcquisitionFrameRate\t80.0\n",
                encoding="utf-8",
            )
            result = generate_camera_config(
                base,
                output_path=root / "usb.yaml",
                camera_make="basler_usb",
                serial="12345678",
                pfs_path=usb_pfs,
                pixel_format="Mono8",
                width=1280,
                height=1024,
                expected_fps=80,
                runtime_frame_rate_override=True,
            )
            data = yaml.safe_load(Path(result.path).read_text(encoding="utf-8"))
            self.assertEqual(result.profile, "usb_mono8_lossless_h264_nvenc_mp4")
            self.assertEqual(data["writer"]["expected_bitrate_mbps"], 900.0)


class SamplingTests(unittest.TestCase):
    def test_capped_source_hashes_span_the_planned_session(self) -> None:
        indices = _source_hash_frame_indices(1000, 10, 3)
        self.assertEqual(indices, frozenset({0, 500, 990}))

    def test_even_decode_sample_includes_session_end(self) -> None:
        self.assertEqual(_evenly_spaced_indices(100, 3), [0, 50, 99])


class ReportTests(unittest.TestCase):
    def _write_session(
        self,
        root: Path,
        *,
        finalized: bool,
        metadata_complete: bool = True,
        omit_last_block_id: bool = False,
    ) -> None:
        (root / "segments").mkdir()
        session = {
            "hardware_fingerprint": {"fingerprint_sha256": "test"},
            "profile_approval": {"approved": True, "reasons": []},
            "resolved": {
                "session": {"duration_s": 1.0},
                "camera": {"expected_fps": 10.0, "expected_pixel_format": "Mono8"},
                "writer": {"queue_max_frames": 100, "ffprobe_path": "ffprobe"},
                "recording_profile": {
                    "id": "usb_mono8_lossless_h264_nvenc_mp4",
                    "pixel_fidelity": "lossless",
                    "validation_status": "requires_hardware_validation",
                },
            },
        }
        (root / "session.json").write_text(json.dumps(session), encoding="utf-8")
        with (root / "frames.csv").open("w", newline="", encoding="utf-8") as handle:
            fields = [
                "frame_index", "camera_block_id", "camera_timestamp_raw", "camera_timestamp_ns",
                "host_receive_perf_counter_ns", "queue_depth_after_enqueue", "dropped_before_frame",
            ]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for index in range(10 if finalized else 2):
                writer.writerow(
                    {
                        "frame_index": index,
                        "camera_block_id": "" if omit_last_block_id and index == 9 else index + 1,
                        "host_receive_perf_counter_ns": index * 100_000_000,
                        "queue_depth_after_enqueue": 1,
                        "dropped_before_frame": 0,
                    }
                )
        with (root / "segments.csv").open("w", newline="", encoding="utf-8") as handle:
            fields = ["segment_id", "path", "first_frame_index", "last_frame_index", "frame_count", "size_bytes"]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            if finalized:
                writer.writerow(
                    {
                        "segment_id": 0,
                        "path": "segments/segment_000000.mp4",
                        "first_frame_index": 0,
                        "last_frame_index": 9,
                        "frame_count": 10,
                        "size_bytes": 100,
                    }
                )
        (root / "experiment_metadata.json").write_text(
            json.dumps({"metadata_complete": metadata_complete, "missing_fixed_fields": [], "fixed_fields": {}}),
            encoding="utf-8",
        )
        if finalized:
            (root / "events.jsonl").write_text(
                json.dumps({"kind": "session_summary", "payload": {"max_queue_depth": 1, "error": ""}}) + "\n",
                encoding="utf-8",
            )
            (root / "pixel_verification.json").write_text(
                json.dumps({"status": "pass_exact_pixel_sample", "hashes_match": True}),
                encoding="utf-8",
            )

    def test_in_progress_session_is_not_labeled_frame_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_session(root, finalized=False)
            report = build_session_report(root)
            self.assertEqual(report["qc"]["status"], "in_progress")
            self.assertEqual(report["qc"]["issues"], [])

    def test_completed_report_has_separate_readiness_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_session(root, finalized=True)
            with mock.patch("pycamrec.report._ffprobe_total_frames", return_value=10):
                report = build_session_report(root)
            self.assertTrue(report["qc"]["acquisition_pass"])
            self.assertTrue(report["qc"]["qc_pass"])
            self.assertTrue(report["qc"]["pixel_pass"])
            self.assertTrue(report["qc"]["metadata_pass"])
            self.assertTrue(report["qc"]["experiment_ready"])

    def test_missing_block_id_cannot_pass_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_session(root, finalized=True, omit_last_block_id=True)
            with mock.patch("pycamrec.report._ffprobe_total_frames", return_value=10):
                report = build_session_report(root)
            self.assertFalse(report["qc"]["acquisition_pass"])
            self.assertEqual(report["qc"]["status"], "fail_frame_integrity")


class PixelVerificationTests(unittest.TestCase):
    def test_pixel_match_is_independent_of_failed_timing_qc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "segments").mkdir()
            session = {
                "resolved": {
                    "writer": {"ffmpeg_path": "ffmpeg", "ffprobe_path": "ffprobe"},
                    "camera": {"expected_pixel_format": "Mono8"},
                    "metadata": {"source_frame_hash_every": 3},
                    "recording_profile": {"id": "lossless", "pixel_fidelity": "lossless"},
                }
            }
            (root / "session.json").write_text(json.dumps(session), encoding="utf-8")
            with (root / "frames.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["frame_index", "source_framemd5"])
                writer.writeheader()
                writer.writerows(
                    [
                        {"frame_index": 0, "source_framemd5": "hash0"},
                        {"frame_index": 1, "source_framemd5": ""},
                        {"frame_index": 2, "source_framemd5": ""},
                        {"frame_index": 3, "source_framemd5": "hash3"},
                    ]
                )
            with (root / "segments.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["path", "first_frame_index", "frame_count"],
                )
                writer.writeheader()
                writer.writerow({"path": "segments/segment_000000.mp4", "first_frame_index": 0, "frame_count": 4})
            (root / "segments" / "segment_000000.mp4").write_bytes(b"video")
            fake_report = {
                "frames": {"expected": 4},
                "recording_profile": session["resolved"]["recording_profile"],
                "qc": {"status": "fail_realtime"},
            }
            with (
                mock.patch("pycamrec.verify.build_session_report", return_value=fake_report),
                mock.patch("pycamrec.verify.ffprobe_video_frames", return_value=4),
                mock.patch(
                    "pycamrec.verify._ffmpeg_gray_framemd5_hashes",
                    return_value=["hash0", "hash3"],
                ) as decode,
            ):
                result = verify_session_pixels(root, max_decode_frames=1)
            self.assertEqual(decode.call_args.kwargs["frame_offsets"], [0, 3])
            self.assertEqual(result.status, "pass_exact_pixel_sample")
            self.assertTrue(result.hashes_match)
            self.assertEqual(result.issues, [])
            self.assertIn("fail_realtime", " ".join(result.acquisition_issues))
            self.assertTrue((root / "pixel_verification.json").is_file())


def _passing_sweep_result(**overrides: object) -> SweepResult:
    values = dict(
        case_id="case",
        duration_s=30.0,
        bitrate_mbps=4000.0,
        bitrate_override=False,
        preview_enabled=False,
        session_dir="session",
        return_code=0,
        qc_status="pass",
        scientific_pass=True,
        acquisition_pass=True,
        qc_pass=True,
        frames=6000,
        expected_frames=6000,
        observed_fps=199.0,
        max_queue_depth=100,
        queue_capacity=1024,
        queue_fraction=100 / 1024,
        preferred_queue_pass=True,
        block_id_gaps=0,
        drop_sum=0,
        metadata_complete=True,
        segment_count=2,
        rollover_pass=True,
        clean_finalization_pass=True,
        segment_mbps=3900.0,
        ffprobe_frames=6000,
        pixel_status="pass_exact_pixel_sample",
        pixel_verified=True,
        pixel_pass=True,
        profile_id="cxp_mono8_lossless_h264_nvenc_mp4",
        pixel_fidelity="lossless",
        lossless_claim=True,
        hardware_fingerprint_sha256="fingerprint",
        technical_pass=True,
        evidence_ready=True,
        profile_approval_pass=True,
        experiment_ready=True,
        error="",
    )
    values.update(overrides)
    return SweepResult(**values)


class ValidationGateTests(unittest.TestCase):
    def test_profile_lock_requires_queue_below_25_percent(self) -> None:
        passing = _passing_sweep_result()
        self.assertIn("lock_validated", _profile_lock_recommendation([passing], True, True))
        high_queue = replace(passing, preferred_queue_pass=False, max_queue_depth=300)
        self.assertEqual(
            _profile_lock_recommendation([high_queue], True, True),
            "do_not_lock_profile_queue_above_25_percent_margin",
        )

    def test_profile_lock_is_preview_mode_specific(self) -> None:
        off = _passing_sweep_result(preview_enabled=False)
        on = _passing_sweep_result(case_id="on", preview_enabled=True)
        self.assertEqual(
            _profile_lock_recommendation([off, on], True, True),
            "do_not_lock_mixed_preview_modes_use_mode_specific_recommendations",
        )

    def test_profile_lock_requires_metadata_and_rollover(self) -> None:
        result = _passing_sweep_result(metadata_complete=False, scientific_pass=False, experiment_ready=False)
        self.assertEqual(
            _profile_lock_recommendation([result], True, True),
            "do_not_lock_profile_metadata_incomplete",
        )
        no_rollover = replace(result, metadata_complete=True, acquisition_pass=False, rollover_pass=False)
        self.assertEqual(
            _profile_lock_recommendation([no_rollover], True, True),
            "do_not_lock_profile_acquisition_or_rollover_failed",
        )

    def test_lock_command_writes_fingerprint_without_mutating_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = ROOT / "configs" / "pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml"
            summary = root / "validation_summary.json"
            result = _passing_sweep_result(profile_approval_pass=False, experiment_ready=False)
            summary.write_text(
                json.dumps(
                    {
                        "profile_lock_by_preview_mode": {
                            "preview_off": "lock_validated_for_this_evidence_fingerprint_preview_off",
                            "preview_on": "no_cases_run",
                        },
                        "results": [asdict(result)],
                    }
                ),
                encoding="utf-8",
            )
            output = root / "approved.yaml"
            report = lock_profile_from_summary(source, summary, output, preview_mode="off")
            approval = yaml.safe_load(output.read_text(encoding="utf-8"))["approval"]
            self.assertEqual(approval["evidence_fingerprint_sha256"], "fingerprint")
            self.assertEqual(report["preview_mode"], "off")
            self.assertEqual(
                yaml.safe_load(source.read_text(encoding="utf-8"))["approval"]["status"],
                "requires_hardware_validation",
            )


class PreviewTests(unittest.TestCase):
    def _worker(self, metrics: dict[str, object]) -> PreviewWorker:
        return PreviewWorker(
            PreviewConfig(enabled=True, shed_queue_fraction=0.10, shed_fps_ratio=0.99),
            source_width=2464,
            source_height=2064,
            source_fps=200,
            source_pixel_format="Mono8",
            queue_max_frames=1024,
            session_dir=ROOT,
            recording_profile_id="test",
            metrics_provider=lambda: metrics,
        )

    def test_preview_sheds_at_10_percent_queue(self) -> None:
        worker = self._worker({"elapsed_s": 3, "frames_grabbed": 600, "expected_fps": 200})
        self.assertEqual(worker._shed_reason(103), "queue")

    def test_setup_preview_is_not_suppressed_by_startup_rate(self) -> None:
        worker = self._worker(
            {"status": "PREVIEW", "elapsed_s": 20, "frames_grabbed": 2500, "expected_fps": 200}
        )
        self.assertEqual(worker._shed_reason(0), "")

    def test_preview_fps_guard_ignores_startup_average(self) -> None:
        metrics = {"status": "REC", "elapsed_s": 3.0, "frames_grabbed": 450, "expected_fps": 200}
        worker = self._worker(metrics)
        self.assertEqual(worker._shed_reason(0), "")
        metrics.update({"elapsed_s": 5.1, "frames_grabbed": 870})
        self.assertEqual(worker._shed_reason(0), "")

    def test_preview_sheds_when_rolling_acquisition_rate_drops(self) -> None:
        metrics = {"status": "REC", "elapsed_s": 3.0, "frames_grabbed": 450, "expected_fps": 200}
        worker = self._worker(metrics)
        self.assertEqual(worker._shed_reason(0), "")
        metrics.update({"elapsed_s": 5.1, "frames_grabbed": 800})
        self.assertEqual(worker._shed_reason(0), "fps")

    def test_mono_setup_preview_uses_downsampled_shared_memory(self) -> None:
        self.assertEqual(_integrated_preview_sink(setup_preview=True, pixel_format="Mono8"), "shm")
        self.assertEqual(_integrated_preview_sink(setup_preview=True, pixel_format="BayerBG8"), "shm_raw")
        self.assertEqual(_integrated_preview_sink(setup_preview=False, pixel_format="BayerBG8"), "shm")


class PackagingAndEstimateTests(unittest.TestCase):
    def test_six_default_configs_load(self) -> None:
        configs = sorted((ROOT / "configs").glob("*.yaml"))
        self.assertEqual(len(configs), 6)
        loaded = [load_config(path, duration_s=1) for path in configs]
        self.assertEqual({cfg.camera.serial for cfg in loaded}, {"24188001", "40559189"})
        self.assertTrue(all(cfg.writer.container == "mp4" for cfg in loaded))

    def test_cxp_cv_config_targets_about_200_mb_per_minute(self) -> None:
        cfg = load_config(
            ROOT / "configs" / "pycamrec_basler_a2A2448_cxp_mono8_cv_optimal.yaml",
            duration_s=60,
        )
        self.assertEqual((cfg.camera.expected_width, cfg.camera.expected_height), (2464, 2064))
        self.assertEqual(cfg.camera.expected_fps, 200.0)
        self.assertEqual(cfg.writer.expected_bitrate_mbps, 27.0)
        self.assertIn("27M", cfg.writer.output_args)
        estimated_mb = cfg.writer.expected_bitrate_mbps * 60 / 8
        self.assertGreaterEqual(estimated_mb, 195)
        self.assertLessEqual(estimated_mb, 210)

    def test_wheel_declares_camera_configs_and_pfs_resources(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        data_files = pyproject["tool"]["setuptools"]["data-files"]
        self.assertEqual(data_files["share/pycamrec/configs"], ["configs/*.yaml"])
        self.assertEqual(len(data_files["share/pycamrec"]), 3)

    def test_disk_estimate_uses_camera_config_override_before_profile_default(self) -> None:
        cfg = SimpleNamespace(
            writer=SimpleNamespace(expected_bitrate_mbps=4000.0),
            recording_profile=SimpleNamespace(expected_bitrate_mbps=900.0),
            session=SimpleNamespace(duration_s=1.0, output_root=ROOT),
            frame_bytes=1,
            expected_total_frames=1,
        )
        disk = SimpleNamespace(free=10 * 1024**3)
        with mock.patch("pycamrec.gui.shutil.disk_usage", return_value=disk):
            text = _disk_estimate_text(cfg)
        self.assertIn("476.8 MiB", text)

    def test_fingerprint_includes_software_and_encoder_version(self) -> None:
        with (
            mock.patch("pycamrec.hardware._gpu_summary", return_value={"nvidia_smi": "gpu, driver"}),
            mock.patch("pycamrec.hardware._pypylon_version", return_value="4.0.0"),
            mock.patch("pycamrec.hardware._git_value", return_value="commit"),
            mock.patch("pycamrec.hardware._git_dirty", return_value=False),
        ):
            first = build_hardware_fingerprint(ffmpeg_version="ffmpeg one")
            second = build_hardware_fingerprint(ffmpeg_version="ffmpeg two")
        self.assertIn("software", first["payload"])
        self.assertNotEqual(first["fingerprint_sha256"], second["fingerprint_sha256"])

    def test_changed_fingerprint_or_preview_mode_invalidates_lock(self) -> None:
        approval = {
            "status": "locked_for_evidence_fingerprint",
            "evidence_fingerprint_sha256": "approved",
            "intended_preview_mode": "off",
        }
        self.assertTrue(
            evaluate_profile_approval(
                approval,
                evidence_fingerprint_sha256="approved",
                preview_enabled=False,
            )["approved"]
        )
        self.assertFalse(
            evaluate_profile_approval(
                approval,
                evidence_fingerprint_sha256="changed",
                preview_enabled=False,
            )["approved"]
        )
        self.assertFalse(
            evaluate_profile_approval(
                approval,
                evidence_fingerprint_sha256="approved",
                preview_enabled=True,
            )["approved"]
        )


class WriterIntegrationTests(unittest.TestCase):
    @staticmethod
    def _ffmpeg_path() -> str | None:
        found = shutil.which("ffmpeg")
        if found:
            return found
        candidates = sorted(Path("C:/ffmpeg").glob("*/bin/ffmpeg.exe"))
        return str(candidates[-1]) if candidates else None

    def test_mp4_rollover_and_finalization(self) -> None:
        ffmpeg = self._ffmpeg_path()
        if ffmpeg is None:
            self.skipTest("FFmpeg is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            segments = root / "segments"
            segments.mkdir()
            cfg = PyCamRecConfig(
                session=SessionConfig(output_root=root, duration_s=0.5),
                camera=CameraConfig(
                    make="basler",
                    serial="test",
                    pfs_path=root / "unused.pfs",
                    expected_width=16,
                    expected_height=16,
                    expected_pixel_format="Mono8",
                    expected_fps=10,
                ),
                writer=WriterConfig(
                    segment_seconds=0.2,
                    ffmpeg_path=ffmpeg,
                    input_pix_fmt="gray",
                    codec="mpeg4",
                    container="mp4",
                    output_pix_fmt="yuv420p",
                    output_args=("-c:v", "mpeg4", "-q:v", "2", "-pix_fmt", "yuv420p"),
                    finalize_timeout_s=30,
                ),
                recording_profile=RecordingProfileConfig(id="integration", pixel_fidelity="lossy"),
                experiment=ExperimentMetadataConfig(),
                metadata=MetadataConfig(),
            )
            writer = FfmpegSegmentWriter(cfg, segments)
            completed = []
            for frame_index in range(5):
                writer.write_frame(frame_index, bytes([frame_index]) * 16 * 16)
                completed.extend(writer.pop_completed_segments())
            final = writer.close()
            if final is not None:
                completed.append(final)
            self.assertEqual([item.frame_count for item in completed], [2, 2, 1])
            self.assertTrue(all(Path(item.path).is_file() and Path(item.path).stat().st_size > 0 for item in completed))
            self.assertEqual(list(segments.glob("*.part.*")), [])
            sampled_hashes = _ffmpeg_gray_framemd5_hashes(
                ffmpeg,
                Path(completed[0].path),
                max_frames=None,
                frame_offsets=[0, 1],
                timeout_s=30,
            )
            self.assertEqual(len(sampled_hashes), 2)


if __name__ == "__main__":
    unittest.main()
