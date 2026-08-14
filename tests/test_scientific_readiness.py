from __future__ import annotations

import csv
import json
import shutil
import tempfile
import unittest
import tomllib
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from pycamrec.acquisition import _source_hash_frame_indices
from pycamrec.approval import lock_profile_from_summary
from pycamrec.config import load_config
from pycamrec.gui import _disk_estimate_text, _integrated_preview_sink
from pycamrec.hardware import (
    build_hardware_fingerprint,
    build_profile_fingerprint,
    evaluate_profile_approval,
)
from pycamrec.onboarding import generate_camera_config
from pycamrec.metadata import MetadataWriter
from pycamrec.preflight import PreflightReport
from pycamrec.preview import PreviewWorker
from pycamrec.report import build_session_report
from pycamrec.qualification import build_qualification_plan, load_task_quality_record
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
from pycamrec.validation_sweep import (
    SweepCase,
    SweepResult,
    _profile_lock_recommendation,
    _write_runtime_config,
)
from pycamrec.session_index import discover_session_dirs, write_session_index
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
        repeat=1,
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
        queue_growth_pass=True,
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
        profile_fingerprint_sha256="profile-fingerprint",
        max_camera_temperature_c=59.0,
        thermal_pass=True,
        storage_pass=True,
        health_pass=True,
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
        passing = [_passing_sweep_result(case_id=f"case-{index}", repeat=index) for index in range(1, 4)]
        self.assertIn("lock_validated", _profile_lock_recommendation(passing, True, True))
        self.assertEqual(
            _profile_lock_recommendation([passing[0]], True, True),
            "do_not_lock_profile_insufficient_repetitions_at_max_duration",
        )
        high_queue = replace(passing[0], preferred_queue_pass=False, max_queue_depth=300)
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

    def test_profile_lock_requires_health_stable_queue_and_repetitions(self) -> None:
        passing = [_passing_sweep_result(case_id=f"case-{index}") for index in range(3)]
        unhealthy = replace(passing[0], health_pass=False, thermal_pass=False)
        self.assertEqual(
            _profile_lock_recommendation([unhealthy], True, True),
            "do_not_lock_profile_health_evidence_failed",
        )
        growing = replace(passing[0], queue_growth_pass=False)
        self.assertEqual(
            _profile_lock_recommendation([growing], True, True),
            "do_not_lock_profile_writer_backlog_still_growing",
        )

    def test_lock_command_writes_fingerprint_without_mutating_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = ROOT / "configs" / "pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml"
            summary = root / "validation_summary.json"
            cfg = load_config(source, duration_s=1)
            profile_fingerprint = build_profile_fingerprint(
                camera_config=asdict(cfg.camera),
                writer_config=asdict(cfg.writer),
                recording_profile=asdict(cfg.recording_profile),
                preview_config=asdict(cfg.preview),
            )["fingerprint_sha256"]
            evidence_session = root / "evidence_session"
            evidence_session.mkdir()
            (evidence_session / "session.json").write_text(
                json.dumps({"resolved": {"preview": asdict(cfg.preview)}}),
                encoding="utf-8",
            )
            results = [
                _passing_sweep_result(
                    case_id=f"case-{index}",
                    repeat=index,
                    session_dir=str(evidence_session),
                    profile_fingerprint_sha256=profile_fingerprint,
                    profile_approval_pass=False,
                    experiment_ready=False,
                )
                for index in range(1, 4)
            ]
            summary.write_text(
                json.dumps(
                    {
                        "profile_lock_by_preview_mode": {
                            "preview_off": "lock_validated_for_this_evidence_fingerprint_preview_off",
                            "preview_on": "no_cases_run",
                        },
                        "requirements": {"passing_repeats_at_max_duration_required": 3},
                        "results": [asdict(result) for result in results],
                    }
                ),
                encoding="utf-8",
            )
            output = root / "approved.yaml"
            report = lock_profile_from_summary(source, summary, output, preview_mode="off")
            approval = yaml.safe_load(output.read_text(encoding="utf-8"))["approval"]
            self.assertEqual(approval["evidence_fingerprint_sha256"], "fingerprint")
            self.assertEqual(approval["profile_fingerprint_sha256"], profile_fingerprint)
            self.assertEqual(approval["validated_max_duration_s"], 30.0)
            self.assertEqual(report["preview_mode"], "off")
            self.assertEqual(
                yaml.safe_load(source.read_text(encoding="utf-8"))["approval"]["status"],
                "requires_hardware_validation",
            )

    def test_preview_on_lock_copies_the_validated_gui_preview_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = ROOT / "configs" / "pycamrec_basler_a2A2448_cxp_mono8_cv_optimal.yaml"
            cfg = load_config(source, duration_s=1)
            preview = asdict(cfg.preview)
            preview.update({"enabled": True, "sink": "shm", "width": 320, "max_fps": 8.0})
            profile_fingerprint = build_profile_fingerprint(
                camera_config=asdict(cfg.camera),
                writer_config=asdict(cfg.writer),
                recording_profile=asdict(cfg.recording_profile),
                preview_config=preview,
            )["fingerprint_sha256"]
            evidence_session = root / "evidence_session"
            evidence_session.mkdir()
            (evidence_session / "session.json").write_text(
                json.dumps({"resolved": {"preview": preview}}),
                encoding="utf-8",
            )
            results = [
                replace(
                    _passing_sweep_result(
                        case_id=f"preview-on-{index}",
                        repeat=index,
                        session_dir=str(evidence_session),
                        preview_enabled=True,
                        profile_fingerprint_sha256=profile_fingerprint,
                    ),
                    profile_id=cfg.recording_profile.id,
                    pixel_fidelity=cfg.recording_profile.pixel_fidelity,
                    lossless_claim=False,
                )
                for index in range(1, 4)
            ]
            summary = root / "validation_summary.json"
            summary.write_text(
                json.dumps(
                    {
                        "profile_lock_by_preview_mode": {
                            "preview_on": "lock_validated_for_this_evidence_fingerprint_preview_on"
                        },
                        "requirements": {"passing_repeats_at_max_duration_required": 3},
                        "results": [asdict(result) for result in results],
                    }
                ),
                encoding="utf-8",
            )
            output = root / "approved-preview-on.yaml"
            lock_profile_from_summary(source, summary, output, preview_mode="on")
            approved_preview = yaml.safe_load(output.read_text(encoding="utf-8"))["preview"]
            self.assertTrue(approved_preview["enabled"])
            self.assertEqual(approved_preview["sink"], "shm")
            self.assertEqual(approved_preview["width"], 320)
            self.assertEqual(approved_preview["max_fps"], 8.0)


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

    def test_cxp_qualification_plan_is_explicit_about_storage(self) -> None:
        plan = build_qualification_plan(
            ROOT / "configs" / "pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml"
        )
        self.assertEqual(plan["required_repetitions_at_max_duration"], 3)
        self.assertEqual(plan["validated_max_duration_s_if_all_pass"], 60)
        self.assertEqual(plan["estimated_total_decimal_gb"], 135.0)
        self.assertIn("--required-passing-repeats 3", plan["powershell_command"])

    def test_lossy_qualification_requires_bound_task_quality_record(self) -> None:
        cfg = load_config(
            ROOT / "configs" / "pycamrec_basler_a2A2448_cxp_mono8_cv_optimal.yaml",
            duration_s=1,
        )
        missing = load_task_quality_record(
            None,
            profile_id=cfg.recording_profile.id,
            profile_version=cfg.recording_profile.version,
            required=True,
        )
        self.assertFalse(missing["pass"])
        plan = build_qualification_plan(
            ROOT / "configs" / "pycamrec_basler_a2A2448_cxp_mono8_cv_optimal.yaml"
        )
        self.assertTrue(plan["task_quality_record_required"])
        self.assertEqual(plan["preview_sink"], "shm")
        self.assertIn("--task-quality-record", plan["powershell_command"])

        with tempfile.TemporaryDirectory() as tmp:
            record_path = Path(tmp) / "task-quality.json"
            record = {
                "schema_version": 1,
                "profile_id": cfg.recording_profile.id,
                "profile_version": cfg.recording_profile.version,
                "status": "pass",
                "reference_dataset_sha256": "a" * 64,
                "acceptance_criteria": {"event_f1_min": 0.95},
                "metrics": {"event_f1": 0.97},
                "evaluated_utc": "2026-08-14T12:00:00Z",
            }
            record_path.write_text(json.dumps(record), encoding="utf-8")
            accepted = load_task_quality_record(
                record_path,
                profile_id=cfg.recording_profile.id,
                profile_version=cfg.recording_profile.version,
                required=True,
            )
            self.assertTrue(accepted["pass"])
            record["evaluated_utc"] = "2026-08-14T12:00:00"
            record_path.write_text(json.dumps(record), encoding="utf-8")
            rejected = load_task_quality_record(
                record_path,
                profile_id=cfg.recording_profile.id,
                profile_version=cfg.recording_profile.version,
                required=True,
            )
            self.assertFalse(rejected["pass"])

    def test_preview_on_qualification_uses_the_gui_shared_memory_sink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime.yaml"
            case = SweepCase(
                case_id="preview_on",
                duration_s=30.0,
                bitrate_mbps=27.0,
                bitrate_override=False,
                preview_enabled=True,
                preview_width=320,
                preview_fps=8.0,
                repeat=1,
                segment_seconds=15.0,
            )
            _write_runtime_config(
                ROOT / "configs" / "pycamrec_basler_a2A2448_cxp_mono8_cv_optimal.yaml",
                runtime,
                case,
                root / "sessions",
                source_frame_hash_every=0,
                source_frame_hash_max_frames=0,
            )
            preview = yaml.safe_load(runtime.read_text(encoding="utf-8"))["preview"]
            self.assertEqual(preview["sink"], "shm")
            self.assertEqual(preview["width"], 320)
            self.assertEqual(preview["max_fps"], 8.0)
            self.assertTrue(Path(preview["image_path"]).is_absolute())

    def test_wheel_declares_camera_configs_and_pfs_resources(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        data_files = pyproject["tool"]["setuptools"]["data-files"]
        packaged_configs = data_files["share/pycamrec/configs"]
        self.assertEqual(len(packaged_configs), 6)
        self.assertNotIn("configs/*.yaml", packaged_configs)
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
            "profile_fingerprint_sha256": "profile",
            "validated_max_duration_s": 60,
            "intended_preview_mode": "off",
        }
        self.assertTrue(
            evaluate_profile_approval(
                approval,
                evidence_fingerprint_sha256="approved",
                profile_fingerprint_sha256="profile",
                preview_enabled=False,
                duration_s=60,
            )["approved"]
        )
        self.assertFalse(
            evaluate_profile_approval(
                approval,
                evidence_fingerprint_sha256="changed",
                profile_fingerprint_sha256="profile",
                preview_enabled=False,
                duration_s=60,
            )["approved"]
        )
        self.assertFalse(
            evaluate_profile_approval(
                approval,
                evidence_fingerprint_sha256="approved",
                profile_fingerprint_sha256="profile",
                preview_enabled=True,
                duration_s=60,
            )["approved"]
        )
        self.assertFalse(
            evaluate_profile_approval(
                approval,
                evidence_fingerprint_sha256="approved",
                profile_fingerprint_sha256="changed",
                preview_enabled=False,
                duration_s=60,
            )["approved"]
        )
        self.assertFalse(
            evaluate_profile_approval(
                approval,
                evidence_fingerprint_sha256="approved",
                profile_fingerprint_sha256="profile",
                preview_enabled=False,
                duration_s=61,
            )["approved"]
        )


class MetadataAndNamingTests(unittest.TestCase):
    def test_typed_metadata_validates_weight_pnd_and_custom_fields(self) -> None:
        today = datetime.now(timezone.utc).date()
        metadata = ExperimentMetadataConfig(
            project_id="vision",
            protocol_id="protocol-1",
            assay_id="open_field",
            subject_id="M012",
            species="mouse",
            date_of_birth=today.isoformat(),
            postnatal_day=0,
            postnatal_day_source="derived_from_date_of_birth",
            weight_g=24.3,
            genotype="wt",
            experimental_group="control",
            sex="female",
            experimenter_id="ms",
            custom_fields={
                "arena_id": {"value": "A03", "value_type": "string"},
                "lighting_lux": {"value": 120, "value_type": "integer", "unit": "lux"},
            },
        )
        self.assertEqual(metadata.missing_fields(), [])
        self.assertEqual(metadata.validation_issues(recording_date=today), [])
        invalid = replace(metadata, weight_g=-1, postnatal_day=1)
        self.assertTrue(any("weight_g" in issue for issue in invalid.validation_issues(recording_date=today)))
        self.assertTrue(any("does not match" in issue for issue in invalid.validation_issues(recording_date=today)))

    def test_session_names_are_stable_friendly_and_recursively_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pfs = root / "camera_12345678.pfs"
            pfs.write_text("Width\t8\nHeight\t8\nPixelFormat\tMono8\n", encoding="utf-8")
            experiment = ExperimentMetadataConfig(
                project_id="Social Vision",
                protocol_id="P-1",
                assay_id="Open Field",
                subject_id="Mouse 012",
                species="mouse",
                postnatal_day=22,
                weight_g=24.3,
                genotype="wt",
                experimental_group="control",
                sex="female",
                experimenter_id="ms",
                custom_fields={"arena_id": {"value": "A03", "value_type": "string"}},
            )
            cfg = PyCamRecConfig(
                session=SessionConfig(output_root=root / "sessions", duration_s=1),
                camera=CameraConfig(
                    make="basler",
                    serial="12345678",
                    pfs_path=pfs,
                    expected_width=8,
                    expected_height=8,
                    expected_pixel_format="Mono8",
                    expected_fps=10,
                ),
                writer=WriterConfig(expected_bitrate_mbps=1),
                recording_profile=RecordingProfileConfig(id="test", pixel_fidelity="lossy"),
                experiment=experiment,
            )
            preflight = PreflightReport(
                pfs_sha256="pfs",
                pfs_features={},
                raw_bytes_per_second=640,
                raw_bytes_total=640,
                estimated_output_bytes=125000,
                estimated_capacity_s_at_target_rate=100,
                output_free_bytes=10**9,
                ffmpeg_version="test",
                warnings=(),
            )
            writer = MetadataWriter(
                cfg,
                preflight,
                {"serial": "12345678", "device_temperature_c": 30},
                evidence_fingerprint={"fingerprint_sha256": "hardware", "payload": {}},
                profile_fingerprint={"fingerprint_sha256": "profile", "payload": {}},
            )
            writer.close(summary={"frames_written": 0})
            relative = writer.session_dir.relative_to(cfg.session.output_root.resolve())
            self.assertEqual(relative.parts[0], "project-social-vision")
            self.assertEqual(relative.parts[1], "subject-mouse-012")
            self.assertIn("__task-open-field__run-001__sid-", relative.parts[-1])
            self.assertEqual(discover_session_dirs(cfg.session.output_root), [writer.session_dir])
            output = root / "sessions.csv"
            report = write_session_index(cfg.session.output_root, output)
            self.assertEqual(report["session_count"], 1)
            with output.open("r", newline="", encoding="utf-8-sig") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["subject_id"], "Mouse 012")
            self.assertEqual(row["weight_g"], "24.3")
            self.assertEqual(row["custom__arena_id"], "A03")


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
