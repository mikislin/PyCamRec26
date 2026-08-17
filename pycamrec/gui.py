"""Windows desktop UI for PyCamRec."""

from __future__ import annotations

import json
import os
import queue as thread_queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import base64
import calendar
import logging
from dataclasses import asdict
from datetime import date, datetime, timezone
from fractions import Fraction
from multiprocessing import shared_memory
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from . import __release_stage__, __version__
from .approval import lock_profile_from_summary
from .config import load_config
from .hardware import build_profile_fingerprint
from .metadata import (
    experiment_metadata_config,
    load_experiment_metadata_json,
    next_run_index,
    normalize_custom_fields,
    record_run_start,
)
from .onboarding import generate_camera_config
from .pixel_formats import is_bayer_pixel_format as is_bayer_camera_pixel_format
from .pixel_formats import is_mono_pixel_format, is_rgb_pixel_format
from .preflight import parse_pfs_features, run_preflight
from .profiles import profile_claims_losslessness
from .report import build_session_report
from .session_index import discover_session_dirs


MODULE_PARENT = Path(__file__).resolve().parents[1]
SOURCE_CHECKOUT = (MODULE_PARENT / "pyproject.toml").is_file()
WORKSPACE_ROOT = MODULE_PARENT if SOURCE_CHECKOUT else Path.cwd().resolve()
RESOURCE_ROOT = MODULE_PARENT if SOURCE_CHECKOUT else Path(sys.prefix) / "share" / "pycamrec"
CONFIG_DIR = RESOURCE_ROOT / "configs"
USER_DATA_ROOT = (
    WORKSPACE_ROOT
    if SOURCE_CHECKOUT
    else Path(os.environ.get("LOCALAPPDATA") or WORKSPACE_ROOT) / "PyCamRec"
)
GENERATED_CONFIG_DIR = CONFIG_DIR / "generated" if SOURCE_CHECKOUT else USER_DATA_ROOT / "configs" / "generated"
DEFAULT_OUTPUT_ROOT = Path("D:/PyCamRecSessions")
RUNTIME_DIR = WORKSPACE_ROOT / ".pycamrec_gui" if SOURCE_CHECKOUT else USER_DATA_ROOT / "runtime"
PREVIEW_IMAGE_PATH = RUNTIME_DIR / "latest_preview.pgm"
RUN_INDEX_STATE_PATH = RUNTIME_DIR / "run_index_state.json"

PROFILE_CONFIGS = (
    CONFIG_DIR / "pycamrec_basler_a2A2448_cxp_mono8_cv_optimal.yaml",
    CONFIG_DIR / "pycamrec_basler_a2A2448_cxp_mono8_near_lossless.yaml",
    CONFIG_DIR / "pycamrec_basler_a2A2448_cxp_mono8_lossless.yaml",
    CONFIG_DIR / "pycamrec_basler_acA1300_usb_mono8_cv_optimal.yaml",
    CONFIG_DIR / "pycamrec_basler_acA1300_usb_mono8_near_lossless.yaml",
    CONFIG_DIR / "pycamrec_basler_acA1300_usb_mono8_lossless.yaml",
)
PROFILE_CHOICES = (
    ("CXP Mono8 CV-optimal candidate", PROFILE_CONFIGS[0]),
    ("CXP Mono8 near-lossless candidate", PROFILE_CONFIGS[1]),
    ("CXP Mono8 lossless candidate", PROFILE_CONFIGS[2]),
    ("USB Mono8 CV-optimal candidate", PROFILE_CONFIGS[3]),
    ("USB Mono8 near-lossless candidate", PROFILE_CONFIGS[4]),
    ("USB Mono8 lossless candidate", PROFILE_CONFIGS[5]),
)
PROFILE_PATH_BY_LABEL = {label: path for label, path in PROFILE_CHOICES}
PROFILE_LABELS = tuple(label for label, _path in PROFILE_CHOICES)
CUSTOM_PROFILE_LABEL = "Custom"
SCIENTIFIC_PREVIEW_PROFILE_IDS = {
    "near_lossless_h264_400m",
    "usb_mono8_lossless_h264_nvenc_mp4",
    "cxp_mono8_lossless_h264_nvenc_mp4",
    "analysis_h264_mp4_100m",
    "analysis_h264_mp4_250m",
    "analysis_h264_mp4_27m",
}

METADATA_FIELDS = (
    ("project_id", "Project ID"),
    ("protocol_id", "Protocol ID"),
    ("assay_id", "Assay / task ID"),
    ("subject_id", "Subject ID"),
    ("species", "Species"),
    ("date_of_birth", "Date of birth (YYYY-MM-DD)"),
    ("postnatal_day", "Postnatal day (P0 = birth date)"),
    ("weight_g", "Weight (g)"),
    ("weight_measured_utc", "Weight measured at (UTC, optional)"),
    ("genotype", "Genotype"),
    ("experimental_group", "Experimental group"),
    ("sex", "Sex"),
    ("experimenter_id", "Experimenter ID"),
    ("run_index", "Run index (automatic)"),
)
REQUIRED_METADATA_FIELDS = {
    "project_id",
    "protocol_id",
    "assay_id",
    "subject_id",
    "species",
    "postnatal_day",
    "weight_g",
    "genotype",
    "experimental_group",
    "sex",
    "experimenter_id",
    "run_index",
}

SESSION_RE = re.compile(
    r"(?:Finalized|Interrupted session finalized|Stopped at)\s+(.+?)\.\s+Camera",
    re.IGNORECASE,
)
PROGRESS_RE = re.compile(
    r"\[PyCamRec\]\s+Progress\s+elapsed_s=(?P<elapsed>[0-9.]+)\s+"
    r"frames=(?P<frames>\d+)/(?P<expected>\d+)\s+written=(?P<written>\d+)\s+"
    r"fps=(?P<fps>[0-9.]+)\s+segment=(?P<segment>\d+)\s+"
    r"queue=(?P<queue>\d+)/(?P<queue_max>\d+)\s+gaps=(?P<gaps>\d+)",
    re.IGNORECASE,
)


class _CalendarDialog:
    """Small dependency-free calendar used for DOB and weight timestamps."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        initial: str,
        include_time: bool,
    ):
        self.parent = parent
        self.include_time = include_time
        now = datetime.now(timezone.utc)
        selected = now.date()
        hour, minute = now.hour, now.minute
        if initial.strip():
            try:
                if include_time:
                    parsed = datetime.fromisoformat(initial.strip().replace("Z", "+00:00"))
                    if parsed.tzinfo is not None:
                        parsed = parsed.astimezone(timezone.utc)
                    selected, hour, minute = parsed.date(), parsed.hour, parsed.minute
                else:
                    selected = date.fromisoformat(initial.strip())
            except ValueError:
                pass
        self.selected_date = selected
        self.visible_year = selected.year
        self.visible_month = selected.month
        self.hour_var = tk.StringVar(value=f"{hour:02d}")
        self.minute_var = tk.StringVar(value=f"{minute:02d}")
        self.result: str | None = None

        self.window = tk.Toplevel(parent)
        self.window.title(title)
        self.window.resizable(False, False)
        self.window.transient(parent.winfo_toplevel())
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)
        self.window.bind("<Escape>", lambda _event: self._cancel())

        navigation = ttk.Frame(self.window, padding=(8, 8, 8, 2))
        navigation.pack(fill=tk.X)
        ttk.Button(navigation, text="<", width=3, command=lambda: self._move_month(-1)).pack(side=tk.LEFT)
        self.month_var = tk.StringVar()
        ttk.Label(navigation, textvariable=self.month_var, anchor=tk.CENTER, width=20).pack(
            side=tk.LEFT, expand=True, padx=8
        )
        ttk.Button(navigation, text=">", width=3, command=lambda: self._move_month(1)).pack(side=tk.RIGHT)

        self.calendar_frame = ttk.Frame(self.window, padding=(8, 2, 8, 4))
        self.calendar_frame.pack(fill=tk.BOTH)

        if include_time:
            time_frame = ttk.Frame(self.window, padding=(8, 2))
            time_frame.pack(fill=tk.X)
            ttk.Label(time_frame, text="Time (UTC)").pack(side=tk.LEFT)
            ttk.Spinbox(time_frame, from_=0, to=23, wrap=True, width=4, textvariable=self.hour_var).pack(
                side=tk.LEFT, padx=(8, 2)
            )
            ttk.Label(time_frame, text=":").pack(side=tk.LEFT)
            ttk.Spinbox(time_frame, from_=0, to=59, wrap=True, width=4, textvariable=self.minute_var).pack(
                side=tk.LEFT, padx=(2, 0)
            )

        actions = ttk.Frame(self.window, padding=(8, 4, 8, 8))
        actions.pack(fill=tk.X)
        ttk.Button(actions, text="Today", command=self._today).pack(side=tk.LEFT)
        ttk.Button(actions, text="Cancel", command=self._cancel).pack(side=tk.RIGHT)
        ttk.Button(
            actions,
            text="Use selected date/time" if include_time else "Use selected date",
            command=self._accept,
        ).pack(side=tk.RIGHT, padx=(0, 8))
        self._render_month()

    def show(self) -> str | None:
        self.window.grab_set()
        self.window.wait_visibility()
        self.window.focus_set()
        self.parent.wait_window(self.window)
        return self.result

    def _render_month(self) -> None:
        for child in self.calendar_frame.winfo_children():
            child.destroy()
        self.month_var.set(f"{calendar.month_name[self.visible_month]} {self.visible_year}")
        for column, label in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
            ttk.Label(self.calendar_frame, text=label, anchor=tk.CENTER, width=4).grid(
                row=0, column=column, padx=1, pady=1
            )
        weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(self.visible_year, self.visible_month)
        for row, week in enumerate(weeks, start=1):
            for column, day_number in enumerate(week):
                if day_number == 0:
                    ttk.Label(self.calendar_frame, text="", width=4).grid(row=row, column=column)
                    continue
                style = "SelectedDay.TButton" if (
                    self.selected_date.year == self.visible_year
                    and self.selected_date.month == self.visible_month
                    and self.selected_date.day == day_number
                ) else "TButton"
                ttk.Button(
                    self.calendar_frame,
                    text=str(day_number),
                    width=4,
                    style=style,
                    command=lambda value=day_number: self._select_day(value),
                ).grid(row=row, column=column, padx=1, pady=1)

    def _select_day(self, day_number: int) -> None:
        self.selected_date = date(self.visible_year, self.visible_month, day_number)
        self._render_month()

    def _move_month(self, offset: int) -> None:
        absolute = self.visible_year * 12 + self.visible_month - 1 + offset
        self.visible_year, month_zero = divmod(absolute, 12)
        self.visible_month = month_zero + 1
        self._render_month()

    def _today(self) -> None:
        now = datetime.now(timezone.utc)
        self.selected_date = now.date()
        self.visible_year, self.visible_month = now.year, now.month
        if self.include_time:
            self.hour_var.set(f"{now.hour:02d}")
            self.minute_var.set(f"{now.minute:02d}")
        self._render_month()

    def _accept(self) -> None:
        if self.include_time:
            try:
                hour = int(self.hour_var.get())
                minute = int(self.minute_var.get())
                selected = datetime.combine(
                    self.selected_date,
                    datetime.min.time(),
                    tzinfo=timezone.utc,
                ).replace(hour=hour, minute=minute)
            except (TypeError, ValueError):
                messagebox.showerror("Invalid time", "Hour must be 0-23 and minute 0-59.", parent=self.window)
                return
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                messagebox.showerror("Invalid time", "Hour must be 0-23 and minute 0-59.", parent=self.window)
                return
            self.result = selected.isoformat(timespec="minutes").replace("+00:00", "Z")
        else:
            self.result = self.selected_date.isoformat()
        self.window.destroy()

    def _cancel(self) -> None:
        self.window.destroy()


class PyCamRecApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"PyCamRec {__version__} ({__release_stage__})")
        self.root.geometry("1320x820")
        self.root.minsize(1100, 680)

        self.ui_queue: thread_queue.Queue[tuple[str, Any]] = thread_queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.setup_preview_process: subprocess.Popen[str] | None = None
        self.validation_process: subprocess.Popen[str] | None = None
        self.reader_thread: threading.Thread | None = None
        self.setup_preview_reader_thread: threading.Thread | None = None
        self.validation_reader_thread: threading.Thread | None = None
        self.record_started_at: float | None = None
        self.record_progress_elapsed_s: float | None = None
        self.record_progress_wall_at: float | None = None
        self.setup_preview_started_at: float | None = None
        self.stop_requested = False
        self.setup_preview_stop_requested = False
        self.setup_preview_previous_preview_enabled: bool | None = None
        self.last_session_dir: Path | None = None
        self.current_runtime_config: Path | None = None
        self.stop_file_path: Path | None = None
        self.setup_preview_stop_file_path: Path | None = None
        self.preview_base_photo: tk.PhotoImage | None = None
        self.preview_photo: tk.PhotoImage | None = None
        self.preview_mtime_ns = 0
        self.preview_image_key = ""
        self.preview_metadata: dict[str, Any] = {}
        self.preview_shm: shared_memory.SharedMemory | None = None
        self.preview_shm_name = ""
        self.preview_render_slot = 0
        self.preview_numpy: Any | None = None
        self.preview_cv2: Any | bool | None = None
        self.run_index_refresh_job: str | None = None
        self.onboard_detected_serial = ""
        self.onboard_detected_capabilities: dict[str, Any] = {}

        self.profile_choice_var = tk.StringVar(value=PROFILE_LABELS[0])
        self.config_var = tk.StringVar(value=str(PROFILE_CONFIGS[0]))
        self.camera_profile_var = tk.StringVar(value="")
        self.output_root_var = tk.StringVar(value=str(DEFAULT_OUTPUT_ROOT))
        self.duration_var = tk.StringVar(value="60")
        self.segment_seconds_var = tk.StringVar(value="")
        self.preview_enabled_var = tk.BooleanVar(value=False)
        self.preview_width_var = tk.StringVar(value="512")
        self.preview_fps_var = tk.StringVar(value="10")
        self.preview_display_mode_var = tk.StringVar(value="Auto")
        self.allow_unspecified_var = tk.BooleanVar(value=False)
        self.onboard_pixel_format_var = tk.StringVar(value="Mono8")
        self.onboard_expected_fps_var = tk.StringVar(value="")
        self.onboard_segment_seconds_var = tk.StringVar(value="120")
        self.onboard_durations_var = tk.StringVar(value="30")
        self.onboard_preview_var = tk.StringVar(value="both")
        self.onboard_repeats_var = tk.StringVar(value="3")
        self.onboard_hash_every_var = tk.StringVar(value="10")
        self.onboard_hash_max_var = tk.StringVar(value="100")
        self.task_quality_record_var = tk.StringVar(value="")
        self.onboard_status_var = tk.StringVar(value="Generate a candidate config, then run evidence validation.")

        self.status_var = tk.StringVar(value="Idle")
        self.device_status_var = tk.StringVar(value="Device status not checked")
        self.monitor_status_var = tk.StringVar(value="Idle")
        self.elapsed_var = tk.StringVar(value="00:00")
        self.run_progress_var = tk.StringVar(value="Frames -- | FPS -- | Queue --")
        self.session_var = tk.StringVar(value="")
        self.qc_status_var = tk.StringVar(value="Not run")
        self.profile_summary_var = tk.StringVar(value="")
        self.profile_qualification_var = tk.StringVar(value="Profile qualification not checked")
        self.setup_readiness_var = tk.StringVar(value="Current recording setup not checked")
        self.disk_estimate_var = tk.StringVar(value="Disk estimate pending")
        self.preview_warning_var = tk.StringVar(value="")
        self.preview_status_var = tk.StringVar(value="Preview panel ready")
        self.metadata_status_var = tk.StringVar(value="Fields default to UNSPECIFIED until filled.")
        self.metadata_source_var = tk.StringVar(value="Metadata entered manually")

        metadata_defaults = {"species": "mouse", "run_index": "1"}
        self.metadata_vars = {
            field_name: tk.StringVar(value=metadata_defaults.get(field_name, ""))
            for field_name, _ in METADATA_FIELDS
        }
        self.notes_text: tk.Text | None = None
        self.custom_fields_text: tk.Text | None = None
        self.start_buttons: list[ttk.Button] = []
        self.preview_start_buttons: list[ttk.Button] = []
        self.preview_stop_buttons: list[ttk.Button] = []
        self.report_summary_vars = {
            "qc": tk.StringVar(value="Not run"),
            "frames": tk.StringVar(value="No report loaded"),
            "fps": tk.StringVar(value="No report loaded"),
            "queue": tk.StringVar(value="No report loaded"),
            "drops": tk.StringVar(value="No report loaded"),
            "segments": tk.StringVar(value="No report loaded"),
            "temperature": tk.StringVar(value="No report loaded"),
            "metadata": tk.StringVar(value="No report loaded"),
            "analysis": tk.StringVar(value="No report loaded"),
        }

        self.logger, self.app_log_path = _make_app_logger()
        self.logger.info("PyCamRec GUI started version=%s stage=%s", __version__, __release_stage__)

        self._configure_style()
        self._build_layout()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._install_state_traces()
        self._load_profile_summary()
        self.refresh_sessions()
        self._poll_ui_queue()
        self._tick_elapsed()
        self._poll_preview_image()

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Header.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Small.TLabel", font=("Segoe UI", 9))
        style.configure("Pass.TLabel", foreground="#087a2b", font=("Segoe UI", 10, "bold"))
        style.configure("Fail.TLabel", foreground="#b00020", font=("Segoe UI", 10, "bold"))
        style.configure("Warn.TLabel", foreground="#9a5b00", font=("Segoe UI", 10, "bold"))
        style.configure("Warning.TLabel", foreground="#9a5b00", font=("Segoe UI", 9, "bold"))
        style.configure("SelectedDay.TButton", font=("Segoe UI", 9, "bold"))

    def _build_layout(self) -> None:
        container = ttk.Frame(self.root, padding=10)
        container.pack(fill=tk.BOTH, expand=True)

        self._build_toolbar(container)

        middle = ttk.PanedWindow(container, orient=tk.HORIZONTAL)
        middle.pack(fill=tk.BOTH, expand=True, pady=(8, 8))

        left_panel = ttk.Frame(middle, width=560)
        right_panel = ttk.Frame(middle, width=700)
        middle.add(left_panel, weight=1)
        middle.add(right_panel, weight=2)

        self.notebook = ttk.Notebook(left_panel)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        self._build_device_tab()
        self._build_setup_tab()
        self._build_metadata_tab()
        self._build_validation_tab()
        self._build_reports_tab()

        self._build_preview_panel(right_panel)
        self._build_output_panel(container)

    def _build_toolbar(self, parent: ttk.Frame) -> None:
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill=tk.X)
        ttk.Label(toolbar, text="Status:").pack(side=tk.LEFT)
        ttk.Label(toolbar, textvariable=self.status_var, style="Status.TLabel").pack(side=tk.LEFT, padx=(4, 18))
        ttk.Label(toolbar, text="Elapsed:").pack(side=tk.LEFT)
        ttk.Label(toolbar, textvariable=self.elapsed_var, style="Status.TLabel").pack(side=tk.LEFT, padx=(4, 18))
        ttk.Label(toolbar, text="Frames:").pack(side=tk.LEFT)
        ttk.Label(toolbar, textvariable=self.run_progress_var, style="Small.TLabel").pack(side=tk.LEFT, padx=(4, 18))
        ttk.Label(toolbar, text="Run:").pack(side=tk.LEFT)
        ttk.Label(toolbar, textvariable=self.monitor_status_var, style="Status.TLabel").pack(side=tk.LEFT, padx=(4, 18))
        ttk.Label(toolbar, text="QC:").pack(side=tk.LEFT)
        self.qc_status_label = ttk.Label(toolbar, textvariable=self.qc_status_var, style="Warn.TLabel")
        self.qc_status_label.pack(side=tk.LEFT, padx=(4, 18))
        ttk.Button(toolbar, text="Preflight", command=self.run_preflight).pack(side=tk.RIGHT, padx=(6, 0))
        self.toolbar_start_button = ttk.Button(toolbar, text="Start recording", command=self.start_recording)
        self.toolbar_start_button.pack(side=tk.RIGHT, padx=(6, 0))
        self.start_buttons.append(self.toolbar_start_button)
        self.stop_button = ttk.Button(toolbar, text="Stop safely", command=self.stop_recording, state=tk.DISABLED)
        self.stop_button.pack(side=tk.RIGHT, padx=(6, 0))

    def _build_device_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Device")

        top = ttk.Frame(frame)
        top.pack(fill=tk.X)
        ttk.Label(top, text="Camera and transport", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Button(top, text="Refresh devices", command=self.refresh_devices).pack(side=tk.RIGHT)

        ttk.Label(frame, textvariable=self.device_status_var, wraplength=500).pack(fill=tk.X, pady=(8, 6))
        self.device_text = tk.Text(frame, height=18, wrap=tk.WORD)
        self.device_text.pack(fill=tk.BOTH, expand=True)
        self.device_text.configure(state=tk.DISABLED)

    def _build_setup_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Setup")

        form = ttk.Frame(frame)
        form.pack(fill=tk.X)

        ttk.Label(form, text="Recording profile").grid(row=0, column=0, sticky=tk.W, pady=3)
        profile_choice_box = ttk.Combobox(
            form,
            textvariable=self.profile_choice_var,
            values=PROFILE_LABELS + (CUSTOM_PROFILE_LABEL,),
            state="readonly",
            width=24,
        )
        profile_choice_box.grid(row=0, column=1, sticky=tk.W, pady=3, padx=(8, 4))
        profile_choice_box.bind("<<ComboboxSelected>>", lambda _event: self._on_profile_choice())

        ttk.Label(form, text="Profile config").grid(row=1, column=0, sticky=tk.W, pady=3)
        ttk.Entry(form, textvariable=self.config_var).grid(row=1, column=1, sticky=tk.EW, pady=3, padx=(8, 4))
        ttk.Button(form, text="Browse", command=self.browse_config).grid(row=1, column=2, sticky=tk.EW)

        ttk.Label(form, text="Camera profile (.pfs)").grid(row=2, column=0, sticky=tk.W, pady=3)
        ttk.Entry(form, textvariable=self.camera_profile_var).grid(row=2, column=1, sticky=tk.EW, pady=3, padx=(8, 4))
        ttk.Button(form, text="Browse", command=self.browse_camera_profile).grid(row=2, column=2, sticky=tk.EW)

        ttk.Label(form, text="Duration (s)").grid(row=3, column=0, sticky=tk.W, pady=3)
        ttk.Entry(form, textvariable=self.duration_var, width=14).grid(row=3, column=1, sticky=tk.W, pady=3, padx=(8, 4))

        ttk.Label(form, text="Segment (s)").grid(row=4, column=0, sticky=tk.W, pady=3)
        ttk.Entry(form, textvariable=self.segment_seconds_var, width=14).grid(
            row=4,
            column=1,
            sticky=tk.W,
            pady=3,
            padx=(8, 4),
        )
        ttk.Label(form, text="blank uses profile default", style="Small.TLabel").grid(
            row=4,
            column=1,
            sticky=tk.W,
            padx=(116, 4),
        )

        ttk.Label(form, text="Output root").grid(row=5, column=0, sticky=tk.W, pady=3)
        ttk.Entry(form, textvariable=self.output_root_var).grid(row=5, column=1, sticky=tk.EW, pady=3, padx=(8, 4))
        ttk.Button(form, text="Browse", command=self.browse_output_root).grid(row=5, column=2, sticky=tk.EW)

        ttk.Label(form, textvariable=self.disk_estimate_var, style="Small.TLabel", wraplength=720).grid(
            row=6,
            column=1,
            columnspan=2,
            sticky=tk.W,
            pady=(0, 4),
            padx=(8, 4),
        )

        preview_frame = ttk.LabelFrame(form, text="Integrated preview")
        preview_frame.grid(row=7, column=0, columnspan=3, sticky=tk.EW, pady=(8, 4))
        ttk.Checkbutton(preview_frame, text="Enable", variable=self.preview_enabled_var).pack(side=tk.LEFT, padx=(8, 8), pady=6)
        ttk.Label(preview_frame, text="Target width").pack(side=tk.LEFT, padx=(8, 4))
        ttk.Entry(preview_frame, textvariable=self.preview_width_var, width=7).pack(side=tk.LEFT)
        ttk.Label(preview_frame, text="FPS").pack(side=tk.LEFT, padx=(12, 4))
        ttk.Entry(preview_frame, textvariable=self.preview_fps_var, width=7).pack(side=tk.LEFT)
        ttk.Label(preview_frame, text="Display").pack(side=tk.LEFT, padx=(12, 4))
        ttk.Combobox(
            preview_frame,
            textvariable=self.preview_display_mode_var,
            values=("Auto", "Raw", "Color"),
            state="readonly",
            width=8,
        ).pack(side=tk.LEFT)
        self.setup_preview_start_button = ttk.Button(preview_frame, text="Start preview", command=self.start_setup_preview)
        self.setup_preview_start_button.pack(side=tk.LEFT, padx=(14, 4))
        self.preview_start_buttons.append(self.setup_preview_start_button)
        self.setup_preview_stop_button = ttk.Button(
            preview_frame,
            text="Stop preview",
            command=self.stop_setup_preview,
            state=tk.DISABLED,
        )
        self.setup_preview_stop_button.pack(side=tk.LEFT, padx=(4, 4))
        self.preview_stop_buttons.append(self.setup_preview_stop_button)
        ttk.Label(preview_frame, text="512/10 is the lightweight live-view target.").pack(
            side=tk.LEFT,
            padx=(14, 4),
        )

        ttk.Label(form, textvariable=self.preview_warning_var, style="Warning.TLabel", wraplength=720).grid(
            row=8,
            column=1,
            columnspan=2,
            sticky=tk.W,
            pady=(0, 4),
            padx=(8, 4),
        )

        ttk.Checkbutton(
            form,
            text="Engineering test: allow UNSPECIFIED metadata",
            variable=self.allow_unspecified_var,
        ).grid(row=9, column=1, columnspan=2, sticky=tk.W, pady=(8, 3), padx=(8, 4))

        form.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Selected profile", style="Header.TLabel").pack(anchor=tk.W, pady=(14, 4))
        ttk.Label(frame, textvariable=self.profile_summary_var, wraplength=520, justify=tk.LEFT).pack(fill=tk.X)

        readiness = ttk.LabelFrame(frame, text="Readiness checks (independent)", padding=8)
        readiness.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(readiness, text="Profile qualification", style="Small.TLabel").grid(
            row=0, column=0, sticky=tk.NW, padx=(0, 8)
        )
        ttk.Label(
            readiness,
            textvariable=self.profile_qualification_var,
            wraplength=410,
            justify=tk.LEFT,
        ).grid(row=0, column=1, sticky=tk.W)
        ttk.Label(readiness, text="Current setup", style="Small.TLabel").grid(
            row=1, column=0, sticky=tk.NW, padx=(0, 8), pady=(6, 0)
        )
        ttk.Label(
            readiness,
            textvariable=self.setup_readiness_var,
            wraplength=410,
            justify=tk.LEFT,
        ).grid(row=1, column=1, sticky=tk.W, pady=(6, 0))
        readiness.columnconfigure(1, weight=1)

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=(18, 0))
        ttk.Button(buttons, text="Preflight", command=self.run_preflight).pack(side=tk.LEFT)
        self.setup_start_button = ttk.Button(buttons, text="Start recording", command=self.start_recording)
        self.setup_start_button.pack(side=tk.LEFT, padx=(8, 0))
        self.start_buttons.append(self.setup_start_button)
        ttk.Button(buttons, text="Open session", command=self.open_last_session_folder).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="Parameters", command=self.open_config_folder).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="Camera profile", command=self.open_camera_profile_folder).pack(side=tk.LEFT, padx=(8, 0))

    def _build_metadata_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Recording metadata")

        ttk.Label(frame, text="Current recording metadata", style="Header.TLabel").grid(
            row=0,
            column=0,
            columnspan=2,
            sticky=tk.W,
            pady=(0, 8),
        )
        ttk.Label(frame, textvariable=self.metadata_source_var, style="Small.TLabel").grid(
            row=0, column=2, columnspan=2, sticky=tk.E, pady=(0, 8)
        )
        field_rows = (len(METADATA_FIELDS) + 1) // 2
        for index, (field_name, label) in enumerate(METADATA_FIELDS):
            form_row = index % field_rows + 1
            base_column = 0 if index < field_rows else 2
            ttk.Label(frame, text=label).grid(row=form_row, column=base_column, sticky=tk.W, pady=3)
            if field_name == "sex":
                widget = ttk.Combobox(
                    frame,
                    textvariable=self.metadata_vars[field_name],
                    values=("female", "male", "intersex", "unknown", "not_applicable"),
                    state="readonly",
                )
            elif field_name in {"date_of_birth", "weight_measured_utc"}:
                widget = ttk.Frame(frame)
                ttk.Entry(widget, textvariable=self.metadata_vars[field_name]).pack(
                    side=tk.LEFT, fill=tk.X, expand=True
                )
                ttk.Button(
                    widget,
                    text="Select...",
                    command=lambda name=field_name: self.select_metadata_date(name),
                ).pack(side=tk.LEFT, padx=(4, 0))
                ttk.Button(
                    widget,
                    text="Today" if field_name == "date_of_birth" else "Now",
                    command=lambda name=field_name: self.set_metadata_date_today(name),
                ).pack(side=tk.LEFT, padx=(4, 0))
            elif field_name == "run_index":
                widget = ttk.Frame(frame)
                ttk.Entry(
                    widget,
                    textvariable=self.metadata_vars[field_name],
                    state="readonly",
                    width=8,
                ).pack(side=tk.LEFT)
                ttk.Button(widget, text="Recalculate", command=self._set_next_run_index).pack(
                    side=tk.LEFT, padx=(4, 0)
                )
            else:
                widget = ttk.Entry(frame, textvariable=self.metadata_vars[field_name])
            widget.grid(
                row=form_row,
                column=base_column + 1,
                sticky=tk.EW,
                pady=3,
                padx=(8, 12),
            )

        custom_row = field_rows + 1
        ttk.Label(frame, text="Custom fields (JSON)").grid(row=custom_row, column=0, sticky=tk.NW, pady=3)
        custom_container = ttk.Frame(frame)
        custom_container.grid(row=custom_row, column=1, columnspan=3, sticky=tk.NSEW, pady=3, padx=(8, 0))
        self.custom_fields_text = tk.Text(custom_container, height=3, wrap=tk.NONE)
        self.custom_fields_text.pack(fill=tk.BOTH, expand=True)
        self.custom_fields_text.insert("1.0", '{}')
        self.custom_fields_text.bind("<FocusOut>", lambda _event: self._schedule_run_index_refresh())
        ttk.Label(
            custom_container,
            text='Shorthand is accepted and typed automatically, e.g. {"arena_id":"A03", "lighting_lux":120}.',
            style="Small.TLabel",
        ).pack(anchor=tk.W)

        notes_row = custom_row + 1
        ttk.Label(frame, text="Notes").grid(row=notes_row, column=0, sticky=tk.NW, pady=3)
        self.notes_text = tk.Text(frame, height=4, wrap=tk.WORD)
        self.notes_text.grid(row=notes_row, column=1, columnspan=3, sticky=tk.NSEW, pady=3, padx=(8, 0))
        self.notes_text.bind("<FocusOut>", lambda _event: self._schedule_run_index_refresh())

        buttons = ttk.Frame(frame)
        buttons.grid(row=notes_row + 1, column=1, columnspan=3, sticky=tk.W, pady=(10, 0), padx=(8, 0))
        ttk.Button(buttons, text="Load JSON...", command=self.load_metadata_json).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Save JSON...", command=self.save_metadata_json).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="Check metadata", command=self.check_metadata).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Reset", command=self.reset_metadata).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(frame, textvariable=self.metadata_status_var, wraplength=500).grid(
            row=notes_row + 2,
            column=1,
            columnspan=3,
            sticky=tk.W,
            pady=(8, 0),
            padx=(8, 0),
        )

        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)
        frame.rowconfigure(notes_row, weight=1)

    def _build_validation_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Profile qualification")

        ttk.Label(frame, text="Recording-profile qualification", style="Header.TLabel").grid(
            row=0,
            column=0,
            columnspan=1,
            sticky=tk.W,
            pady=(0, 8),
        )
        ttk.Label(
            frame,
            text=(
                "This tab validates encoder/camera performance and can create evidence for a hardware-specific "
                "profile lock. It does not supply metadata for an experimental recording."
            ),
            wraplength=650,
            justify=tk.LEFT,
        ).grid(row=0, column=1, columnspan=2, sticky=tk.W, pady=(0, 8), padx=(8, 0))
        ttk.Label(frame, text="Pixel type").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.onboard_pixel_combo = ttk.Combobox(
            frame,
            textvariable=self.onboard_pixel_format_var,
            values=("Mono8", "BayerBG8", "RGB8", "BGR8"),
            state="readonly",
            width=16,
        )
        self.onboard_pixel_combo.grid(row=1, column=1, sticky=tk.W, pady=3, padx=(8, 4))
        self.onboard_pixel_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_onboard_pixel_format_changed())
        ttk.Label(frame, text="Camera PFS").grid(row=2, column=0, sticky=tk.W, pady=3)
        ttk.Entry(frame, textvariable=self.camera_profile_var).grid(
            row=2,
            column=1,
            sticky=tk.EW,
            pady=3,
            padx=(8, 4),
        )
        ttk.Button(frame, text="Browse", command=self.browse_camera_profile).grid(row=2, column=2, sticky=tk.W)
        ttk.Label(frame, text="Expected FPS").grid(row=3, column=0, sticky=tk.W, pady=3)
        ttk.Entry(frame, textvariable=self.onboard_expected_fps_var, width=16).grid(
            row=3,
            column=1,
            sticky=tk.W,
            pady=3,
            padx=(8, 4),
        )
        ttk.Label(frame, text="Segment size (s)").grid(row=4, column=0, sticky=tk.W, pady=3)
        ttk.Entry(frame, textvariable=self.onboard_segment_seconds_var, width=16).grid(
            row=4,
            column=1,
            sticky=tk.W,
            pady=3,
            padx=(8, 4),
        )
        ttk.Label(frame, text="Sweep durations").grid(row=5, column=0, sticky=tk.W, pady=3)
        ttk.Entry(frame, textvariable=self.onboard_durations_var, width=24).grid(
            row=5,
            column=1,
            sticky=tk.W,
            pady=3,
            padx=(8, 4),
        )
        ttk.Label(frame, text="Preview").grid(row=6, column=0, sticky=tk.W, pady=3)
        ttk.Combobox(
            frame,
            textvariable=self.onboard_preview_var,
            values=("both", "on", "off"),
            state="readonly",
            width=16,
        ).grid(row=6, column=1, sticky=tk.W, pady=3, padx=(8, 4))
        ttk.Label(frame, text="Required repetitions").grid(row=7, column=0, sticky=tk.W, pady=3)
        ttk.Entry(frame, textvariable=self.onboard_repeats_var, width=16).grid(
            row=7, column=1, sticky=tk.W, pady=3, padx=(8, 4)
        )
        ttk.Label(frame, text="Hash every / max").grid(row=8, column=0, sticky=tk.W, pady=3)
        hash_frame = ttk.Frame(frame)
        hash_frame.grid(row=8, column=1, sticky=tk.W, pady=3, padx=(8, 4))
        ttk.Entry(hash_frame, textvariable=self.onboard_hash_every_var, width=8).pack(side=tk.LEFT)
        ttk.Label(hash_frame, text="/").pack(side=tk.LEFT, padx=4)
        ttk.Entry(hash_frame, textvariable=self.onboard_hash_max_var, width=8).pack(side=tk.LEFT)

        ttk.Label(frame, text="Task-quality record").grid(row=9, column=0, sticky=tk.W, pady=3)
        ttk.Entry(frame, textvariable=self.task_quality_record_var).grid(
            row=9, column=1, sticky=tk.EW, pady=3, padx=(8, 4)
        )
        ttk.Button(frame, text="Browse", command=self.browse_task_quality_record).grid(
            row=9, column=2, sticky=tk.W
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=10, column=1, columnspan=2, sticky=tk.W, pady=(12, 4), padx=(8, 4))
        ttk.Button(buttons, text="Detect camera caps", command=self.detect_camera_capabilities).grid(
            row=0, column=0, sticky=tk.W
        )
        ttk.Button(buttons, text="Generate candidate config", command=self.generate_onboarding_config).grid(
            row=0, column=1, sticky=tk.W, padx=(8, 0)
        )
        ttk.Button(buttons, text="Run qualification sweep", command=self.run_onboarding_sweep).grid(
            row=0, column=2, sticky=tk.W, padx=(8, 0)
        )
        ttk.Button(buttons, text="Create locked config...", command=self.create_locked_config).grid(
            row=1, column=0, sticky=tk.W, pady=(6, 0)
        )
        ttk.Button(buttons, text="Verify last pixels", command=self.verify_last_session_pixels).grid(
            row=1, column=1, sticky=tk.W, padx=(8, 0), pady=(6, 0)
        )
        ttk.Button(buttons, text="Open sweeps", command=self.open_validation_sweeps).grid(
            row=1, column=2, sticky=tk.W, padx=(8, 0), pady=(6, 0)
        )

        ttk.Label(frame, textvariable=self.onboard_status_var, wraplength=560, justify=tk.LEFT).grid(
            row=11,
            column=0,
            columnspan=3,
            sticky=tk.W,
            pady=(10, 4),
        )
        ttk.Label(
            frame,
            text=(
                "Reliability target: expected frame count, ffprobe count, no gaps/drops, "
                ">=98% expected FPS, queue below 90% and preferably below 25%, clean finalization, "
                "pixel verification, and complete metadata before experiments."
            ),
            wraplength=650,
            justify=tk.LEFT,
            style="Small.TLabel",
        ).grid(row=12, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))
        frame.columnconfigure(1, weight=1)

    def _build_reports_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Reports")

        top = ttk.Frame(frame)
        top.pack(fill=tk.X)
        ttk.Button(top, text="Refresh", command=self.refresh_sessions).pack(side=tk.LEFT)
        ttk.Button(top, text="Review report", command=self.generate_selected_report).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(top, text="Save report JSON...", command=self.save_selected_report).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(top, text="Open folder", command=self.open_selected_session_folder).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(top, text="Open video segments", command=self.open_selected_video_segments).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(top, text="Open metadata", command=self.open_selected_metadata).pack(side=tk.LEFT, padx=(8, 0))

        self.session_list = tk.Listbox(frame, height=6)
        self.session_list.pack(fill=tk.X, pady=(8, 6))
        self.session_list.bind("<<ListboxSelect>>", lambda _event: self._select_session_from_list())

        self.report_notebook = ttk.Notebook(frame)
        self.report_notebook.pack(fill=tk.BOTH, expand=True)

        summary_frame = ttk.Frame(self.report_notebook, padding=6)
        raw_frame = ttk.Frame(self.report_notebook, padding=6)
        self.report_notebook.add(summary_frame, text="Summary")
        self.report_notebook.add(raw_frame, text="Raw JSON")

        cards = ttk.Frame(summary_frame)
        cards.pack(fill=tk.X)
        card_specs = (
            ("qc", "QC status"),
            ("frames", "Frames"),
            ("fps", "Timing"),
            ("queue", "Writer queue"),
            ("drops", "Drops / gaps"),
            ("segments", "Segments"),
            ("temperature", "Temperature"),
            ("metadata", "Metadata"),
            ("analysis", "Analysis read"),
        )
        for index, (key, title) in enumerate(card_specs):
            card = ttk.LabelFrame(cards, text=title, padding=8)
            row, column = divmod(index, 4)
            card.grid(row=row, column=column, sticky=tk.NSEW, padx=4, pady=4)
            ttk.Label(card, textvariable=self.report_summary_vars[key], wraplength=210, justify=tk.LEFT).pack(
                fill=tk.BOTH,
                expand=True,
            )
        for column in range(4):
            cards.columnconfigure(column, weight=1)

        ttk.Label(summary_frame, text="Issues and warnings", style="Header.TLabel").pack(anchor=tk.W, pady=(10, 4))
        self.report_issues_text = tk.Text(summary_frame, height=8, wrap=tk.WORD)
        self.report_issues_text.pack(fill=tk.BOTH, expand=True)
        self.report_issues_text.configure(state=tk.DISABLED)

        self.report_text = tk.Text(raw_frame, height=18, wrap=tk.WORD)
        self.report_text.pack(fill=tk.BOTH, expand=True)
        self.report_text.configure(state=tk.DISABLED)

    def _build_preview_panel(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        header = ttk.Frame(parent)
        header.grid(row=0, column=0, sticky=tk.EW)
        ttk.Label(header, text="Live preview", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Label(header, textvariable=self.preview_status_var, style="Small.TLabel").pack(side=tk.RIGHT)

        self.preview_frame = tk.Frame(parent, background="#050505", highlightthickness=1, highlightbackground="#333333")
        self.preview_frame.grid(row=1, column=0, sticky=tk.NSEW, pady=(6, 0))
        self.preview_frame.columnconfigure(0, weight=1)
        self.preview_frame.rowconfigure(0, weight=1)
        self.preview_frame.bind("<Configure>", lambda _event: self._rescale_preview_image())
        self.preview_label = tk.Label(
            self.preview_frame,
            text="Preview disabled\nEnable integrated preview in Setup",
            foreground="#bfbfbf",
            background="#050505",
            font=("Segoe UI", 13),
            justify=tk.CENTER,
        )
        self.preview_label.grid(row=0, column=0, sticky=tk.NSEW)

    def _build_output_panel(self, parent: ttk.Frame) -> None:
        output_frame = ttk.Frame(parent)
        output_frame.pack(fill=tk.BOTH, expand=False)
        header = ttk.Frame(output_frame)
        header.pack(fill=tk.X)
        ttk.Label(header, text="Command output", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Button(header, text="Open app log", command=self.open_app_log).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Label(header, textvariable=self.session_var, style="Small.TLabel").pack(side=tk.RIGHT)

        self.monitor_text = tk.Text(output_frame, height=12, wrap=tk.WORD)
        self.monitor_text.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.monitor_text.configure(state=tk.DISABLED)
        self._append_output(f"App log: {self.app_log_path}\n")

    def _install_state_traces(self) -> None:
        for var in (
            self.duration_var,
            self.segment_seconds_var,
            self.output_root_var,
            self.config_var,
            self.camera_profile_var,
            self.preview_width_var,
            self.preview_fps_var,
        ):
            var.trace_add("write", lambda *_args: self.root.after_idle(self._load_profile_summary))
        self.preview_enabled_var.trace_add("write", lambda *_args: self.root.after_idle(self._load_profile_summary))
        self.allow_unspecified_var.trace_add("write", lambda *_args: self.root.after_idle(self._refresh_start_state))
        for field_name, var in self.metadata_vars.items():
            var.trace_add("write", lambda *_args: self.root.after_idle(self._refresh_start_state))
            if field_name != "run_index":
                var.trace_add("write", lambda *_args: self._schedule_run_index_refresh())
        self.output_root_var.trace_add("write", lambda *_args: self._schedule_run_index_refresh())

    def _schedule_run_index_refresh(self) -> None:
        if self.run_index_refresh_job is not None:
            try:
                self.root.after_cancel(self.run_index_refresh_job)
            except tk.TclError:
                pass
        self.run_index_refresh_job = self.root.after(350, self._refresh_run_index_silently)

    def _refresh_run_index_silently(self) -> None:
        self.run_index_refresh_job = None
        self._set_next_run_index(show_error=False)

    def _on_profile_choice(self) -> None:
        path = PROFILE_PATH_BY_LABEL.get(self.profile_choice_var.get())
        if path is not None:
            self.config_var.set(str(path))
            self.camera_profile_var.set("")
            try:
                cfg = load_config(path, duration_s=self._duration_s(), output_root=self.output_root_var.get())
            except Exception:
                pass
            else:
                self.onboard_pixel_format_var.set(str(cfg.camera.expected_pixel_format))
                self.onboard_expected_fps_var.set(f"{cfg.camera.expected_fps:g}")
                self.onboard_segment_seconds_var.set(f"{cfg.writer.segment_seconds:g}")
                self.segment_seconds_var.set("")
        self._load_profile_summary()

    def _refresh_start_state(self) -> None:
        process_running = self.process is not None and self.process.poll() is None
        setup_preview_running = self._setup_preview_running()
        validation_running = self.validation_process is not None and self.validation_process.poll() is None
        missing = self._missing_metadata_fields()
        semantic_issues: list[str] = []
        try:
            experiment = experiment_metadata_config(self._metadata_values())
            semantic_issues = experiment.validation_issues(
                recording_date=datetime.now(timezone.utc).date()
            )
        except Exception as exc:
            semantic_issues = [str(exc)]
        metadata_ok = (not missing or self.allow_unspecified_var.get()) and not semantic_issues
        state = tk.NORMAL if metadata_ok and not process_running and not setup_preview_running and not validation_running else tk.DISABLED
        for button in self.start_buttons:
            button.configure(state=state)
        preview_start_state = tk.DISABLED if process_running or setup_preview_running or validation_running else tk.NORMAL
        preview_stop_state = tk.NORMAL if setup_preview_running else tk.DISABLED
        for button in self.preview_start_buttons:
            button.configure(state=preview_start_state)
        for button in self.preview_stop_buttons:
            button.configure(state=preview_stop_state)
        if process_running:
            self.setup_readiness_var.set("RECORDING — setup is frozen until safe finalization.")
            return
        if setup_preview_running:
            self.metadata_status_var.set("Setup preview is active. Stop preview before recording.")
            self.setup_readiness_var.set("SETUP PREVIEW — metadata is not being recorded.")
            return
        if semantic_issues:
            self.metadata_status_var.set("Metadata invalid: " + "; ".join(semantic_issues))
            self.setup_readiness_var.set("BLOCKED — metadata values are inconsistent or invalid.")
        elif missing and self.allow_unspecified_var.get():
            self.metadata_status_var.set("Engineering mode: metadata incomplete but recording is allowed.")
            self.setup_readiness_var.set(
                "ENGINEERING ONLY — incomplete metadata is allowed; output is not experiment-ready."
            )
        elif missing:
            self.metadata_status_var.set("Start disabled until metadata is complete: " + ", ".join(missing))
            self.setup_readiness_var.set(
                f"BLOCKED — complete {len(missing)} required metadata field(s) in Recording metadata."
            )
        else:
            self.metadata_status_var.set("Metadata complete.")
            self.setup_readiness_var.set(
                "METADATA READY — run Preflight to check storage, PFS, encoder, and current profile coverage."
            )

    def refresh_devices(self) -> None:
        self.device_status_var.set("Checking pylon devices...")
        self._set_text(self.device_text, "Checking devices in a short-lived subprocess...\n")
        threading.Thread(target=self._refresh_devices_worker, daemon=True).start()

    def _refresh_devices_worker(self) -> None:
        try:
            report = _run_pycamrec_json(["devices"], timeout_s=45)
        except Exception as exc:
            report = {"devices": [], "interfaces": [], "transport_layers": [], "error": repr(exc)}
        self.ui_queue.put(("devices", report))

    def browse_config(self) -> None:
        path = filedialog.askopenfilename(
            initialdir=str(CONFIG_DIR),
            title="Select PyCamRec profile config",
            filetypes=(("YAML files", "*.yaml *.yml"), ("All files", "*.*")),
        )
        if path:
            self.config_var.set(path)
            self.profile_choice_var.set(CUSTOM_PROFILE_LABEL)
            self.camera_profile_var.set("")
            self._load_profile_summary()

    def browse_camera_profile(self) -> None:
        path = filedialog.askopenfilename(
            initialdir=str(WORKSPACE_ROOT),
            title="Select Basler camera .pfs profile",
            filetypes=(("Basler PFS files", "*.pfs"), ("All files", "*.*")),
        )
        if path:
            self.camera_profile_var.set(path)
            self._load_profile_summary()

    def browse_task_quality_record(self) -> None:
        path = filedialog.askopenfilename(
            initialdir=str(WORKSPACE_ROOT / "qualification"),
            title="Select task-quality evidence JSON",
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
        )
        if path:
            self.task_quality_record_var.set(path)

    def browse_output_root(self) -> None:
        path = filedialog.askdirectory(initialdir=self.output_root_var.get() or str(DEFAULT_OUTPUT_ROOT))
        if path:
            self.output_root_var.set(path)
            self.refresh_sessions()
            self._load_profile_summary()

    def load_metadata_json(self) -> None:
        path = filedialog.askopenfilename(
            initialdir=str(self.last_session_dir or WORKSPACE_ROOT),
            title="Load experiment metadata",
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
        )
        if not path:
            return
        try:
            document = load_experiment_metadata_json(Path(path))
            self._apply_metadata_document(document)
            self._set_next_run_index()
        except Exception as exc:
            messagebox.showerror("Could not load metadata", str(exc))
            return
        self.metadata_source_var.set(f"Loaded: {Path(path).name}")
        self.check_metadata()

    def save_metadata_json(self) -> None:
        try:
            self._set_next_run_index()
            document = self._metadata_values()
        except Exception as exc:
            messagebox.showerror("Could not prepare metadata", str(exc))
            return
        path = filedialog.asksaveasfilename(
            initialdir=str(WORKSPACE_ROOT),
            initialfile="experiment_metadata.json",
            defaultextension=".json",
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
            title="Save experiment metadata",
        )
        if not path:
            return
        try:
            Path(path).write_text(
                json.dumps(document, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:
            messagebox.showerror("Could not save metadata", str(exc))
            return
        self.metadata_source_var.set(f"Saved: {Path(path).name}")

    def _apply_metadata_document(self, document: dict[str, Any]) -> None:
        project = document.get("project") or {}
        subject = document.get("subject") or {}
        acquisition = document.get("acquisition") or {}
        values = {
            "project_id": project.get("project_id"),
            "protocol_id": project.get("protocol_id"),
            "assay_id": project.get("assay_id"),
            "subject_id": subject.get("subject_id"),
            "species": subject.get("species"),
            "date_of_birth": subject.get("date_of_birth"),
            "postnatal_day": subject.get("postnatal_day"),
            "weight_g": subject.get("weight_g"),
            "weight_measured_utc": subject.get("weight_measured_utc"),
            "genotype": subject.get("genotype"),
            "experimental_group": subject.get("experimental_group"),
            "sex": subject.get("sex"),
            "experimenter_id": acquisition.get("experimenter_id"),
            "run_index": acquisition.get("run_index", 1),
        }
        for field_name, value in values.items():
            self.metadata_vars[field_name].set("" if value is None else str(value))
        if self.custom_fields_text is not None:
            self.custom_fields_text.delete("1.0", tk.END)
            self.custom_fields_text.insert(
                "1.0",
                json.dumps(document.get("custom_fields") or {}, indent=2, sort_keys=True),
            )
        if self.notes_text is not None:
            self.notes_text.delete("1.0", tk.END)
            self.notes_text.insert("1.0", str(document.get("notes") or ""))

    def select_metadata_date(self, field_name: str) -> None:
        include_time = field_name == "weight_measured_utc"
        dialog = _CalendarDialog(
            self.root,
            title="Weight measurement date/time" if include_time else "Date of birth",
            initial=self.metadata_vars[field_name].get(),
            include_time=include_time,
        )
        selected = dialog.show()
        if selected is None:
            return
        self.metadata_vars[field_name].set(selected)
        if field_name == "date_of_birth":
            self._derive_postnatal_day_from_dob()
        self._refresh_start_state()

    def set_metadata_date_today(self, field_name: str) -> None:
        now = datetime.now(timezone.utc)
        if field_name == "date_of_birth":
            self.metadata_vars[field_name].set(now.date().isoformat())
            self._derive_postnatal_day_from_dob()
        else:
            self.metadata_vars[field_name].set(
                now.isoformat(timespec="seconds").replace("+00:00", "Z")
            )
        self._refresh_start_state()

    def _derive_postnatal_day_from_dob(self) -> None:
        try:
            birth_date = date.fromisoformat(self.metadata_vars["date_of_birth"].get().strip())
        except ValueError:
            return
        age_days = (datetime.now(timezone.utc).date() - birth_date).days
        if age_days >= 0:
            self.metadata_vars["postnatal_day"].set(str(age_days))

    def _set_next_run_index(self, *, show_error: bool = True) -> int | None:
        try:
            if not self.metadata_vars["run_index"].get().strip():
                self.metadata_vars["run_index"].set("1")
            document = self._metadata_values()
            run_index = next_run_index(
                Path(self.output_root_var.get()),
                document,
                state_path=RUN_INDEX_STATE_PATH,
            )
        except Exception as exc:
            if show_error:
                self.setup_readiness_var.set(f"Run index unavailable: {exc}")
            return None
        self.metadata_vars["run_index"].set(str(run_index))
        return run_index

    def generate_onboarding_config(self) -> None:
        try:
            base_config = Path(self.config_var.get()).expanduser().resolve()
            cfg = load_config(base_config, duration_s=self._duration_s(), output_root=self.output_root_var.get())
            pixel_format = self.onboard_pixel_format_var.get().strip() or cfg.camera.expected_pixel_format
            expected_fps_text = self.onboard_expected_fps_var.get().strip()
            expected_fps = float(expected_fps_text) if expected_fps_text else cfg.camera.expected_fps
            camera_profile_path = self._onboarding_pfs_path(pixel_format, cfg)
            camera_make = self._onboarding_camera_make(cfg)
            expected_width, expected_height = self._onboarding_dimensions(cfg, camera_profile_path)
            runtime_frame_rate_override = self._pfs_frame_rate_mismatch(camera_profile_path, expected_fps)
            report = generate_camera_config(
                base_config,
                output_dir=GENERATED_CONFIG_DIR,
                serial=self.onboard_detected_serial or None,
                camera_make=camera_make,
                pfs_path=camera_profile_path,
                pixel_format=pixel_format,
                expected_fps=expected_fps,
                width=expected_width,
                height=expected_height,
                segment_seconds=float(self.onboard_segment_seconds_var.get()),
                runtime_pixel_format_override=True,
                runtime_frame_rate_override=runtime_frame_rate_override,
            )
        except Exception as exc:
            self.onboard_status_var.set(f"Config generation failed: {exc}")
            messagebox.showerror("Config generation failed", str(exc))
            return
        self.config_var.set(report.path)
        self.camera_profile_var.set(report.pfs_path)
        self.profile_choice_var.set(CUSTOM_PROFILE_LABEL)
        self.segment_seconds_var.set(str(report.segment_seconds))
        self.onboard_status_var.set(
            f"Generated candidate config:\n{report.path}\n"
            "Status is generated_unvalidated until a validation sweep passes on this hardware fingerprint."
        )
        self._append_output("\n=== Generated camera config ===\n" + json.dumps(report.to_dict(), indent=2) + "\n")
        self._load_profile_summary()

    def detect_camera_capabilities(self) -> None:
        try:
            cfg = load_config(self.config_var.get(), duration_s=self._duration_s(), output_root=self.output_root_var.get())
            report = _run_pycamrec_json(["camera-capabilities", "--serial", cfg.camera.serial], timeout_s=45)
        except Exception as exc:
            self.onboard_status_var.set(
                f"Active config could not be loaded ({exc}); probing the first available camera."
            )
            report = _run_pycamrec_json(["camera-capabilities"], timeout_s=45)
        if report.get("error"):
            fallback = _run_pycamrec_json(["camera-capabilities"], timeout_s=45)
            if not fallback.get("error"):
                report = fallback
        self._append_output("\n=== Camera capabilities ===\n" + json.dumps(_jsonable(report), indent=2, sort_keys=True) + "\n")
        if report.get("error"):
            self.onboard_status_var.set(f"Capability probe failed: {report.get('error')}")
            return
        self.onboard_detected_capabilities = report
        self.onboard_detected_serial = str(report.get("serial") or "")
        pixel_formats = [str(item) for item in report.get("pixel_formats") or []]
        if pixel_formats and hasattr(self, "onboard_pixel_combo"):
            self.onboard_pixel_combo.configure(values=tuple(pixel_formats))
            preferred = _preferred_pixel_format(pixel_formats)
            self.onboard_pixel_format_var.set(preferred)
        fps_value = _suggested_onboarding_fps(report, self.onboard_pixel_format_var.get())
        if fps_value is not None:
            self.onboard_expected_fps_var.set(f"{float(fps_value):g}")
        matched_pfs = _matching_pfs_for_camera(self.onboard_detected_serial, self.onboard_pixel_format_var.get())
        if matched_pfs is not None:
            self.camera_profile_var.set(str(matched_pfs))
        self.onboard_status_var.set(
            f"Camera capabilities detected for serial {self.onboard_detected_serial or 'unknown'}. "
            "Generated configs will use the detected serial, full-frame dimensions, and selected/matching PFS; "
            "validation starts preview-on first."
        )

    def _on_onboard_pixel_format_changed(self) -> None:
        pixel_format = self.onboard_pixel_format_var.get()
        matched_pfs = _matching_pfs_for_camera(self.onboard_detected_serial, pixel_format)
        if matched_pfs is not None:
            self.camera_profile_var.set(str(matched_pfs))
        fps_value = _suggested_onboarding_fps(self.onboard_detected_capabilities, pixel_format)
        if fps_value is not None:
            self.onboard_expected_fps_var.set(f"{float(fps_value):g}")
        if matched_pfs is not None:
            self.onboard_status_var.set(
                f"Selected {pixel_format}; using matching PFS {matched_pfs.name}. "
                "Generate config, then validate preview-on first."
            )

    def run_onboarding_sweep(self) -> None:
        if self.validation_process is not None and self.validation_process.poll() is None:
            messagebox.showinfo("Validation active", "A validation sweep is already running.")
            return
        if self.process is not None and self.process.poll() is None:
            messagebox.showinfo("Recording active", "Stop recording before running validation.")
            return
        if self._setup_preview_running():
            messagebox.showinfo("Preview active", "Stop setup preview before running validation.")
            return
        try:
            config_path = self._write_runtime_config(preview_enabled_override=False)
            cfg = load_config(config_path)
            hash_every = int(float(self.onboard_hash_every_var.get()))
            hash_max = int(float(self.onboard_hash_max_var.get()))
            repeats = int(self.onboard_repeats_var.get())
            if repeats < 3:
                raise ValueError("Scientific qualification requires at least three repetitions.")
            durations = [
                float(value.strip())
                for value in self.onboard_durations_var.get().split(",")
                if value.strip()
            ]
            if not durations or any(value <= 0 for value in durations):
                raise ValueError("Sweep durations must be positive comma-separated seconds.")
            preview_case_count = 2 if self.onboard_preview_var.get() == "both" else 1
            bitrate = cfg.writer.expected_bitrate_mbps or cfg.recording_profile.expected_bitrate_mbps
            estimated_bytes = (
                sum(durations) * repeats * preview_case_count * float(bitrate) * 1_000_000 / 8
                if bitrate
                else None
            )
            if max(durations) > 30:
                estimate = _format_bytes(estimated_bytes) if estimated_bytes is not None else "unknown"
                if not messagebox.askyesno(
                    "Confirm storage-intensive validation",
                    f"This qualification includes runs longer than 30 seconds.\n\n"
                    f"Cases: {len(durations) * repeats * preview_case_count}\n"
                    f"Estimated total output: {estimate}\n"
                    f"Required repetitions: {repeats}\n\n"
                    "Continue only after confirming free space and camera cooling.",
                ):
                    return
            command = [
                sys.executable,
                "-m",
                "pycamrec",
                "validation-sweep",
                str(config_path),
                "--output-root",
                self.output_root_var.get(),
                "--durations",
                self.onboard_durations_var.get().strip(),
                "--preview",
                self.onboard_preview_var.get(),
                "--preview-order",
                "on-first",
                "--preview-width",
                self.preview_width_var.get(),
                "--preview-fps",
                self.preview_fps_var.get(),
                "--repeats",
                str(repeats),
                "--required-passing-repeats",
                str(repeats),
            ]
            if profile_claims_losslessness(cfg.recording_profile.pixel_fidelity):
                command.extend(
                    [
                        "--verify-session-pixels",
                        "--pixel-max-decode-frames",
                        "1000",
                        "--source-frame-hash-every",
                        str(hash_every),
                        "--source-frame-hash-max-frames",
                        str(hash_max),
                    ]
                )
            qualification = cfg.raw.get("qualification") if isinstance(cfg.raw, dict) else {}
            task_quality_required = bool(
                isinstance(qualification, dict)
                and qualification.get("require_task_quality_record", False)
            )
            task_quality_path = self.task_quality_record_var.get().strip()
            if task_quality_required and not task_quality_path:
                raise ValueError(
                    "This lossy profile requires a passing task-quality record before qualification."
                )
            if task_quality_path:
                command.extend(["--task-quality-record", task_quality_path])
            if self.allow_unspecified_var.get():
                command.append("--allow-unspecified-metadata")
            else:
                command.append("--require-complete-metadata")
        except Exception as exc:
            messagebox.showerror("Could not prepare validation", str(exc))
            return
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            self.validation_process = subprocess.Popen(
                command,
                cwd=str(WORKSPACE_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=creationflags,
                env=_child_env(),
            )
        except Exception as exc:
            self.validation_process = None
            messagebox.showerror("Could not start validation", str(exc))
            return
        self.status_var.set("Validation sweep")
        self.onboard_status_var.set("Validation sweep running. Cases are appended to the command output.")
        self._append_output("\n=== Validation sweep started ===\n" + " ".join(command) + "\n")
        self.validation_reader_thread = threading.Thread(target=self._read_validation_output, daemon=True)
        self.validation_reader_thread.start()

    def create_locked_config(self) -> None:
        summary_path = filedialog.askopenfilename(
            initialdir=str(WORKSPACE_ROOT / "validation_sweeps"),
            title="Select passing validation_summary.json",
            filetypes=(("Validation summary", "validation_summary.json"), ("JSON files", "*.json")),
        )
        if not summary_path:
            return
        preview_choice = messagebox.askyesnocancel(
            "Lock preview mode",
            "Create a lock for preview ON?\n\nYes = preview on\nNo = preview off\nCancel = stop",
        )
        if preview_choice is None:
            return
        preview_mode = "on" if preview_choice else "off"
        approved_dir = WORKSPACE_ROOT / "configs" / "approved"
        approved_dir.mkdir(parents=True, exist_ok=True)
        source_stem = Path(self.config_var.get()).stem
        output_path = filedialog.asksaveasfilename(
            initialdir=str(approved_dir),
            initialfile=f"{source_stem}_preview_{preview_mode}_approved.yaml",
            defaultextension=".yaml",
            filetypes=(("YAML files", "*.yaml"), ("All files", "*.*")),
            title="Save hardware-specific locked config",
        )
        if not output_path:
            return
        try:
            result = lock_profile_from_summary(
                Path(self.config_var.get()),
                Path(summary_path),
                Path(output_path),
                preview_mode=preview_mode,
            )
        except Exception as exc:
            messagebox.showerror("Profile was not lockable", str(exc))
            return
        self.config_var.set(str(Path(output_path).resolve()))
        self.profile_choice_var.set(CUSTOM_PROFILE_LABEL)
        self.camera_profile_var.set("")
        self.preview_enabled_var.set(preview_choice)
        self.onboard_status_var.set(
            f"Created and selected {preview_mode} locked config:\n{output_path}\n"
            "Recording startup will still verify the live hardware/software fingerprint."
        )
        self._append_output("\n=== Locked profile config ===\n" + json.dumps(result, indent=2) + "\n")
        self._load_profile_summary()

    def verify_last_session_pixels(self) -> None:
        session = self._selected_or_last_session()
        if session is None:
            messagebox.showinfo("No session", "Select a session or complete a recording first.")
            return
        try:
            report = _run_pycamrec_json(["verify-session-pixels", str(session), "--max-decode-frames", "1000"], timeout_s=1800)
        except Exception as exc:
            messagebox.showerror("Pixel verification failed", str(exc))
            return
        text = json.dumps(_jsonable(report), indent=2, sort_keys=True)
        self._append_output("\n=== Session pixel verification ===\n" + text + "\n")
        self.onboard_status_var.set(
            f"Pixel verification for {session.name}: {report.get('status', 'unknown')}"
        )

    def reset_metadata(self) -> None:
        for field_name, _ in METADATA_FIELDS:
            self.metadata_vars[field_name].set(
                "mouse" if field_name == "species" else "1" if field_name == "run_index" else ""
            )
        if self.custom_fields_text is not None:
            self.custom_fields_text.delete("1.0", tk.END)
            self.custom_fields_text.insert("1.0", "{}")
        if self.notes_text is not None:
            self.notes_text.delete("1.0", tk.END)
        self.metadata_source_var.set("Metadata entered manually")
        self.metadata_status_var.set("Fields reset to UNSPECIFIED.")
        self._set_next_run_index()
        self._refresh_start_state()

    def check_metadata(self) -> bool:
        try:
            runtime_config = self._write_runtime_config()
            cfg = load_config(runtime_config)
            issues = cfg.experiment.readiness_issues(
                recording_date=datetime.now(timezone.utc).date()
            )
        except Exception as exc:
            self.metadata_status_var.set(f"Invalid metadata: {exc}")
            self._refresh_start_state()
            return False
        if issues:
            self.metadata_status_var.set("Metadata issues: " + "; ".join(issues))
            self._refresh_start_state()
            return False
        self.metadata_status_var.set("Metadata complete.")
        self._refresh_start_state()
        return True

    def run_preflight(self) -> None:
        try:
            runtime_config = self._write_runtime_config()
            cfg = load_config(runtime_config)
            report = run_preflight(cfg)
        except Exception as exc:
            self._append_output(f"ERROR: {exc}\n")
            messagebox.showerror("Preflight failed", str(exc))
            return

        text = json.dumps(_jsonable(asdict(report)), indent=2, sort_keys=True)
        self._append_output("\n=== Preflight ===\n" + text + "\n")
        self.logger.info("Preflight completed with %s warning(s)", len(report.warnings))
        if report.warnings:
            self.status_var.set(f"Preflight: {len(report.warnings)} warning(s)")
            self.setup_readiness_var.set(
                f"PREFLIGHT COMPLETE — {len(report.warnings)} advisory warning(s); review the command output. "
                "The recorder verifies the camera and any approval certificate after opening the device."
            )
        else:
            self.status_var.set("Preflight OK")
            self.setup_readiness_var.set(
                "PREFLIGHT PASSED — storage, PFS, encoder, and metadata checks passed. "
                "Any hardware-specific profile lock is verified after the camera opens."
            )

    def start_setup_preview(self) -> None:
        if self._setup_preview_running():
            messagebox.showinfo("Preview active", "Setup preview is already running.")
            return
        if self.process is not None and self.process.poll() is None:
            messagebox.showinfo("Recording active", "Stop recording before starting setup preview.")
            return

        try:
            runtime_config = self._write_runtime_config(preview_enabled_override=True, setup_preview=True)
            cfg = load_config(runtime_config)
            device_report = _run_pycamrec_json(["devices"], timeout_s=45)
        except Exception as exc:
            self.logger.exception("Could not prepare setup preview")
            messagebox.showerror("Could not prepare preview", str(exc))
            return
        if not _device_report_has_serial(device_report, cfg.camera.serial):
            self._set_text(self.device_text, json.dumps(_jsonable(device_report), indent=2, sort_keys=True) + "\n")
            self.device_status_var.set("Camera not available to preview subprocess")
            self.notebook.select(0)
            messagebox.showerror(
                "Camera not available",
                f"The preview subprocess cannot see camera serial {cfg.camera.serial}. "
                "Close pylon Viewer or any other camera app, then refresh devices.",
            )
            return

        self.setup_preview_stop_file_path = RUNTIME_DIR / f"preview_stop_{int(time.time())}_{runtime_config.stem}.txt"
        try:
            self.setup_preview_stop_file_path.unlink()
        except OSError:
            pass
        command = [
            sys.executable,
            "-m",
            "pycamrec",
            "preview",
            str(runtime_config),
            "--stop-file",
            str(self.setup_preview_stop_file_path),
        ]
        self._clear_preview_image()
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            self.setup_preview_process = subprocess.Popen(
                command,
                cwd=str(WORKSPACE_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=creationflags,
                env=_child_env(),
            )
        except Exception as exc:
            self.setup_preview_process = None
            self.logger.exception("Could not start setup preview")
            messagebox.showerror("Could not start preview", str(exc))
            return

        self.setup_preview_started_at = time.perf_counter()
        self.setup_preview_stop_requested = False
        self.setup_preview_previous_preview_enabled = bool(self.preview_enabled_var.get())
        self.status_var.set("Setup preview")
        self.monitor_status_var.set("Preview")
        self.preview_enabled_var.set(True)
        self._refresh_start_state()
        self._append_output("\n=== Setup preview started ===\n" + " ".join(command) + "\n")
        self.logger.info("Setup preview started: %s", " ".join(command))
        self.setup_preview_reader_thread = threading.Thread(target=self._read_setup_preview_output, daemon=True)
        self.setup_preview_reader_thread.start()

    def stop_setup_preview(self, wait: bool = False) -> bool:
        if self.setup_preview_process is not None and self.setup_preview_process.poll() is not None:
            self._handle_setup_preview_done(int(self.setup_preview_process.returncode or 0))
            return True
        if self.setup_preview_process is None:
            self.status_var.set("No active setup preview")
            self._refresh_start_state()
            return True
        if not self.setup_preview_stop_requested:
            self.setup_preview_stop_requested = True
            self.status_var.set("Stopping preview")
            self.monitor_status_var.set("Stopping preview")
            self._append_output("Setup preview stop requested. Releasing camera...\n")
            self.logger.info("Setup preview stop requested")
            try:
                if self.setup_preview_stop_file_path is None:
                    raise RuntimeError("Setup preview stop-file path is not available.")
                self.setup_preview_stop_file_path.parent.mkdir(parents=True, exist_ok=True)
                self.setup_preview_stop_file_path.write_text("stop\n", encoding="utf-8")
            except Exception as exc:
                self._append_output(f"WARNING: Could not request preview stop: {exc}\n")
                self.logger.warning("Could not request preview stop: %r", exc)
        if wait:
            try:
                return_code = self.setup_preview_process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._append_output("WARNING: Setup preview did not stop within 8 seconds.\n")
                self.logger.warning("Setup preview did not stop within timeout")
                return False
            self._handle_setup_preview_done(return_code)
        return True

    def start_recording(self) -> None:
        if self.process is not None and self.process.poll() is None:
            messagebox.showinfo("Recording active", "A recording process is already running.")
            return
        if self._setup_preview_running():
            if not self.stop_setup_preview(wait=True):
                messagebox.showerror("Preview still active", "Stop setup preview before recording.")
                return
        missing = self._missing_metadata_fields()
        if missing and not self.allow_unspecified_var.get():
            self.metadata_status_var.set("Missing: " + ", ".join(missing))
            self.notebook.select(2)
            messagebox.showwarning(
                "Metadata incomplete",
                "Fill metadata before scientific recording, or enable engineering test mode.",
            )
            return

        try:
            run_index = self._set_next_run_index()
            if run_index is None:
                raise ValueError("Could not calculate the next run index.")
            runtime_config = self._write_runtime_config()
            cfg = load_config(runtime_config)
            device_report = _run_pycamrec_json(["devices"], timeout_s=45)
        except Exception as exc:
            messagebox.showerror("Could not prepare recording", str(exc))
            return
        if not _device_report_has_serial(device_report, cfg.camera.serial):
            self._set_text(self.device_text, json.dumps(_jsonable(device_report), indent=2, sort_keys=True) + "\n")
            self.device_status_var.set("Camera not available to recorder subprocess")
            self.notebook.select(0)
            messagebox.showerror(
                "Camera not available",
                f"The recorder subprocess cannot see camera serial {cfg.camera.serial}. "
                "Close pylon Viewer or any other camera app, then refresh devices.",
            )
            return
        time.sleep(0.5)

        command = [sys.executable, "-m", "pycamrec", "record", str(runtime_config)]
        if self.allow_unspecified_var.get():
            command.append("--allow-unspecified-metadata")
        self.stop_file_path = RUNTIME_DIR / f"stop_{int(time.time())}_{runtime_config.stem}.txt"
        try:
            self.stop_file_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        command.extend(["--stop-file", str(self.stop_file_path)])
        self._clear_preview_image()

        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(WORKSPACE_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=creationflags,
                env=_child_env(),
            )
        except Exception as exc:
            self.process = None
            messagebox.showerror("Could not start recording", str(exc))
            return

        try:
            record_run_start(
                RUN_INDEX_STATE_PATH,
                cfg.raw.get("experiment") if isinstance(cfg.raw, dict) else self._metadata_values(),
                cfg.experiment.run_index,
            )
        except Exception as exc:
            self.logger.warning("Could not persist automatic run index: %r", exc)
            self._append_output(f"WARNING: Could not persist automatic run index: {exc}\n")

        self.current_runtime_config = runtime_config
        self.record_started_at = time.perf_counter()
        self.record_progress_elapsed_s = None
        self.record_progress_wall_at = None
        self.stop_requested = False
        self.last_session_dir = None
        self.session_var.set("")
        self.status_var.set("Recording")
        self.monitor_status_var.set("Recording")
        self.elapsed_var.set("00:00")
        self.run_progress_var.set(
            f"0/{cfg.expected_total_frames} | 0.0 fps | queue 0/{cfg.writer.queue_max_frames}"
        )
        self.preview_status_var.set(
            f"00:00 | 0/{cfg.expected_total_frames} | 0.0 fps | queue 0/{cfg.writer.queue_max_frames}"
        )
        self._reset_run_qc_state()
        for button in self.start_buttons:
            button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self._append_output("\n=== Recording started ===\n" + " ".join(command) + "\n")
        self.logger.info("Recording started: %s", " ".join(command))

        self.reader_thread = threading.Thread(target=self._read_process_output, daemon=True)
        self.reader_thread.start()

    def stop_recording(self) -> None:
        if self.process is None or self.process.poll() is not None:
            self.status_var.set("No active recording")
            return
        if self.stop_requested:
            self._append_output("Stop already requested. Waiting for finalization...\n")
            return
        self.stop_requested = True
        self.status_var.set("Stopping safely")
        self.monitor_status_var.set("Stopping safely")
        self._append_output("Safe stop requested. Waiting for queue drain and segment finalization...\n")
        try:
            if self.stop_file_path is None:
                raise RuntimeError("Stop-file path is not available for this recording.")
            self.stop_file_path.parent.mkdir(parents=True, exist_ok=True)
            self.stop_file_path.write_text("stop\n", encoding="utf-8")
        except Exception as exc:
            self._append_output(f"WARNING: Could not request safe stop: {exc}\n")

    def _read_process_output(self) -> None:
        assert self.process is not None
        if self.process.stdout is not None:
            for line in self.process.stdout:
                self.ui_queue.put(("record_line", line))
        return_code = self.process.wait()
        self.ui_queue.put(("record_done", return_code))

    def _read_setup_preview_output(self) -> None:
        assert self.setup_preview_process is not None
        if self.setup_preview_process.stdout is not None:
            for line in self.setup_preview_process.stdout:
                self.ui_queue.put(("preview_line", line))
        return_code = self.setup_preview_process.wait()
        self.ui_queue.put(("preview_done", return_code))

    def _read_validation_output(self) -> None:
        assert self.validation_process is not None
        if self.validation_process.stdout is not None:
            for line in self.validation_process.stdout:
                self.ui_queue.put(("validation_line", line))
        return_code = self.validation_process.wait()
        self.ui_queue.put(("validation_done", return_code))

    def refresh_sessions(self) -> None:
        if not hasattr(self, "session_list"):
            return
        root = Path(self.output_root_var.get()).expanduser()
        self.session_list.delete(0, tk.END)
        if not root.exists():
            return
        sessions = discover_session_dirs(root)
        for session in sessions[:250]:
            self.session_list.insert(tk.END, str(session))

    def generate_last_report(self) -> None:
        if self.last_session_dir is None:
            messagebox.showinfo("No session", "No completed session folder is known yet.")
            return
        self._show_report(self.last_session_dir)

    def generate_selected_report(self) -> None:
        session = self._selected_session()
        if session is None:
            messagebox.showinfo("No session", "Select a session first.")
            return
        self._show_report(session)

    def save_selected_report(self) -> None:
        session = self._selected_or_last_session()
        if session is None:
            messagebox.showinfo("No session", "Select a session first.")
            return
        try:
            report = build_session_report(session)
        except Exception as exc:
            messagebox.showerror("Report failed", str(exc))
            return
        output_path = filedialog.asksaveasfilename(
            initialdir=str(session),
            initialfile="scientific_report.json",
            defaultextension=".json",
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
            title="Save scientific report",
        )
        if not output_path:
            return
        path = Path(output_path)
        if path.exists():
            messagebox.showerror(
                "Report already exists",
                "PyCamRec will not overwrite an existing report. Choose a new filename.",
            )
            return
        try:
            path.write_text(
                json.dumps(_jsonable(report), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            messagebox.showerror("Could not save report", str(exc))
            return
        self._display_report(session, report, append_output=False)
        self.status_var.set(f"Report saved: {path.name}")

    def _show_report(self, session_dir: Path) -> None:
        try:
            report = build_session_report(session_dir)
        except Exception as exc:
            messagebox.showerror("Report failed", str(exc))
            return
        self._display_report(session_dir, report, append_output=True)
        self.notebook.select(4)
        self.report_notebook.select(0)

    def _display_report(self, session_dir: Path, report: dict[str, Any], append_output: bool) -> None:
        text = json.dumps(_jsonable(report), indent=2, sort_keys=True)
        self._set_text(self.report_text, text + "\n")
        self._render_report_summary(report)
        self._set_qc_status(report)
        self.session_var.set(str(session_dir))
        if append_output:
            self._append_output("\n=== Report ===\n" + text + "\n")

    def _render_report_summary(self, report: dict[str, Any]) -> None:
        frames = report.get("frames", {})
        host_timing = report.get("host_timing", {})
        queue = report.get("queue", {})
        segments = report.get("segments", {})
        health = report.get("health", {})
        experiment = report.get("experiment_metadata", {})
        analysis = report.get("analysis", {})
        qc = report.get("qc", {})

        count = frames.get("count")
        expected = frames.get("expected")
        block_gaps = frames.get("block_id_gaps") or 0
        drop_sum = frames.get("drop_sum") or 0
        self.report_summary_vars["frames"].set(
            f"{count}/{expected} frames\nblock gaps {block_gaps}"
        )
        observed = host_timing.get("approx_fps")
        expected_fps = qc.get("expected_fps")
        fps_text = _format_number(observed, " fps")
        if expected_fps is not None:
            fps_text += f"\nexpected {_format_number(expected_fps, ' fps')}"
        self.report_summary_vars["fps"].set(fps_text)
        self.report_summary_vars["queue"].set(
            f"max {queue.get('max_depth', 0)}\nrows at max {queue.get('rows_at_max_depth', 0)}"
        )
        self.report_summary_vars["drops"].set(
            f"drops {drop_sum}\ngaps {block_gaps}"
        )
        self.report_summary_vars["segments"].set(
            f"{segments.get('count', 0)} segment(s)\n"
            f"{_format_bytes(float(segments.get('total_bytes') or 0))}\n"
            f"{_format_number(segments.get('approx_mbps'), ' Mbps')}"
        )
        self.report_summary_vars["temperature"].set(
            f"last {_format_number(health.get('last_camera_temperature_c'), ' C')}\n"
            f"max {_format_number(health.get('max_camera_temperature_c'), ' C')}"
        )
        metadata_complete = bool(experiment.get("metadata_complete"))
        missing = experiment.get("missing_fixed_fields") or []
        self.report_summary_vars["metadata"].set(
            "complete" if metadata_complete else "incomplete\n" + ", ".join(str(item) for item in missing[:4])
        )
        self.report_summary_vars["analysis"].set(
            f"{analysis.get('source_pixel_format') or 'unknown'} | "
            f"{analysis.get('channel_semantics') or 'unknown'}\n"
            f"{(analysis.get('recommendations') or {}).get('preferred_mode') or 'see manifest'}"
        )
        self.report_summary_vars["qc"].set(_qc_display_text(str(qc.get("status") or "unknown")))

        issues = [str(item) for item in (qc.get("issues") or [])]
        warnings = [str(item) for item in (qc.get("warnings") or [])]
        lines = []
        if issues:
            lines.append("Issues:")
            lines.extend(f"- {item}" for item in issues)
        if warnings:
            if lines:
                lines.append("")
            lines.append("Warnings:")
            lines.extend(f"- {item}" for item in warnings)
        self._set_text(self.report_issues_text, ("\n".join(lines) if lines else "No QC issues or warnings.") + "\n")

    def _set_qc_status(self, report: dict[str, Any]) -> None:
        status = str((report.get("qc") or {}).get("status") or "unknown")
        self.qc_status_var.set(_qc_display_text(status))
        if status == "pass":
            self.qc_status_label.configure(style="Pass.TLabel")
        elif status.startswith("fail"):
            self.qc_status_label.configure(style="Fail.TLabel")
        else:
            self.qc_status_label.configure(style="Warn.TLabel")

    def _reset_run_qc_state(self) -> None:
        self.qc_status_var.set("RUNNING")
        self.qc_status_label.configure(style="Warn.TLabel")
        for key, var in self.report_summary_vars.items():
            var.set("Pending current run" if key == "qc" else "No current report")

    def open_last_session_folder(self) -> None:
        if self.last_session_dir is None:
            messagebox.showinfo("No session", "No completed session folder is known yet.")
            return
        _open_path(self.last_session_dir)

    def open_selected_session_folder(self) -> None:
        session = self._selected_session()
        if session is not None:
            _open_path(session)

    def open_selected_video_segments(self) -> None:
        session = self._selected_or_last_session()
        if session is None:
            messagebox.showinfo("No session", "Select a session first.")
            return
        segments_dir = session / "segments"
        _open_path(segments_dir if segments_dir.exists() else session)

    def open_selected_metadata(self) -> None:
        session = self._selected_or_last_session()
        if session is None:
            messagebox.showinfo("No session", "Select a session first.")
            return
        metadata_path = session / "experiment_metadata.json"
        _open_path(metadata_path if metadata_path.exists() else session / "session.json")

    def open_app_log(self) -> None:
        _open_path(self.app_log_path)

    def open_config_folder(self) -> None:
        _open_path(Path(self.config_var.get()).expanduser().resolve().parent)

    def open_validation_sweeps(self) -> None:
        path = WORKSPACE_ROOT / "validation_sweeps"
        path.mkdir(parents=True, exist_ok=True)
        _open_path(path)

    def open_camera_profile_folder(self) -> None:
        camera_profile = self.camera_profile_var.get().strip()
        if camera_profile:
            _open_path(Path(camera_profile).expanduser().resolve().parent)
            return
        try:
            cfg = load_config(self.config_var.get(), duration_s=self._duration_s(), output_root=self.output_root_var.get())
        except Exception as exc:
            messagebox.showerror("Could not find camera profile", str(exc))
            return
        _open_path(cfg.camera.pfs_path.parent)

    def _select_session_from_list(self) -> None:
        session = self._selected_session()
        if session is not None:
            self.session_var.set(str(session))

    def _selected_session(self) -> Path | None:
        selection = self.session_list.curselection()
        if not selection:
            return None
        return Path(self.session_list.get(selection[0]))

    def _selected_or_last_session(self) -> Path | None:
        return self._selected_session() or self.last_session_dir

    def _setup_preview_running(self) -> bool:
        return self.setup_preview_process is not None and self.setup_preview_process.poll() is None

    def _load_profile_summary(self) -> None:
        try:
            cfg = load_config(
                self.config_var.get(),
                duration_s=self._duration_s(),
                output_root=self.output_root_var.get(),
                preview_enabled=self.preview_enabled_var.get(),
                preview_width=self._preview_width(),
                preview_max_fps=float(self.preview_fps_var.get()),
            )
        except Exception as exc:
            self.profile_summary_var.set(f"Could not load profile: {exc}")
            self.profile_qualification_var.set("INVALID — selected profile config could not be loaded.")
            self.disk_estimate_var.set("Disk estimate unavailable until the profile loads.")
            self.preview_warning_var.set("")
            self._refresh_start_state()
            return
        if not self.camera_profile_var.get().strip():
            self.camera_profile_var.set(str(cfg.camera.pfs_path))
        if not self.onboard_pixel_format_var.get().strip():
            self.onboard_pixel_format_var.set(str(cfg.camera.expected_pixel_format))
        if not self.onboard_expected_fps_var.get().strip():
            self.onboard_expected_fps_var.set(f"{cfg.camera.expected_fps:g}")
        if not self.onboard_segment_seconds_var.get().strip():
            self.onboard_segment_seconds_var.set(f"{cfg.writer.segment_seconds:g}")
        qualification = cfg.raw.get("qualification") if isinstance(cfg.raw, dict) else {}
        if isinstance(qualification, dict) and qualification:
            durations = qualification.get("durations_s") or []
            if durations:
                self.onboard_durations_var.set(",".join(f"{float(value):g}" for value in durations))
            preview_modes = [str(value).lower() for value in qualification.get("preview_modes") or []]
            if set(preview_modes) == {"off", "on"}:
                self.onboard_preview_var.set("both")
            elif preview_modes:
                self.onboard_preview_var.set(preview_modes[0])
            if qualification.get("required_repeats") is not None:
                self.onboard_repeats_var.set(str(int(qualification["required_repeats"])))
        profile = cfg.recording_profile
        try:
            segment_seconds = self._segment_seconds(fallback=cfg.writer.segment_seconds)
        except Exception:
            segment_seconds = cfg.writer.segment_seconds
        self.profile_summary_var.set(
            f"{profile.display_name} | {profile.pixel_fidelity} | "
            f"target {cfg.writer.expected_bitrate_mbps} Mbps\n"
            f"Camera {cfg.camera.make} serial {cfg.camera.serial} | "
            f"{cfg.camera.expected_pixel_format} {cfg.camera.expected_width}x{cfg.camera.expected_height} "
            f"@ {cfg.camera.expected_fps:g} fps | output .{cfg.writer.container} | segment {segment_seconds:g}s\n"
            f"Temperature warning/critical {cfg.camera.temperature_warning_c:g}/"
            f"{cfg.camera.temperature_critical_c:g} C; health checks every "
            f"{cfg.camera.health_check_interval_s:g}s. {profile.recommended_use}"
        )
        self.profile_qualification_var.set(
            _profile_qualification_text(
                cfg,
                camera_profile_path=self.camera_profile_var.get().strip() or None,
                segment_seconds=segment_seconds,
            )
        )
        self.disk_estimate_var.set(_disk_estimate_text(cfg))
        self.preview_warning_var.set(_preview_warning_text(cfg))
        self._refresh_start_state()

    def _write_runtime_config(
        self,
        preview_enabled_override: bool | None = None,
        *,
        setup_preview: bool = False,
    ) -> Path:
        import yaml

        base_path = Path(self.config_var.get()).expanduser().resolve()
        data = yaml.safe_load(base_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Config file must contain a mapping: {base_path}")

        data = dict(data)
        session = dict(data.get("session", {}))
        session["duration_s"] = self._duration_s()
        session["output_root"] = self.output_root_var.get()
        data["session"] = session

        writer = dict(data.get("writer", {}))
        writer["segment_seconds"] = self._segment_seconds()
        data["writer"] = writer

        camera = dict(data.get("camera", {}))
        camera_profile = self.camera_profile_var.get().strip()
        if camera_profile:
            self._validate_camera_profile_override(base_path, camera, camera_profile)
            camera["pfs_path"] = camera_profile
        data["camera"] = camera

        preview = dict(data.get("preview", {}))
        preview["enabled"] = bool(self.preview_enabled_var.get() if preview_enabled_override is None else preview_enabled_override)
        preview["width"] = self._preview_width()
        preview["max_fps"] = float(self.preview_fps_var.get())
        preview["sink"] = _integrated_preview_sink(
            setup_preview=setup_preview,
            pixel_format=str(camera.get("expected_pixel_format") or ""),
        )
        preview["image_path"] = str(PREVIEW_IMAGE_PATH)
        data["preview"] = preview

        data["experiment"] = self._metadata_values()
        runtime_overrides = dict(data.get("runtime_overrides", {}))
        runtime_overrides.update(
            {
                "source_config": str(base_path),
                "duration_s": session["duration_s"],
                "output_root": session["output_root"],
                "camera_profile_path": camera_profile,
                "preview_enabled": preview["enabled"],
                "preview_sink": preview["sink"],
            }
        )
        data["runtime_overrides"] = runtime_overrides

        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        runtime_path = RUNTIME_DIR / f"runtime_{int(time.time())}_{base_path.stem}.yaml"
        runtime_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return runtime_path

    def _onboarding_pfs_path(self, pixel_format: str, cfg: Any) -> Path:
        detected_serial = self.onboard_detected_serial.strip()
        selected = self.camera_profile_var.get().strip()
        if detected_serial:
            if selected and detected_serial in Path(selected).name:
                return Path(selected).expanduser().resolve()
            matched = _matching_pfs_for_camera(detected_serial, pixel_format)
            if matched is not None:
                self.camera_profile_var.set(str(matched))
                return matched
            raise ValueError(
                f"No .pfs file matching detected camera serial {detected_serial!r} was found. "
                "Export or browse the camera's PFS before generating a config."
            )
        return Path(selected).expanduser().resolve() if selected else cfg.camera.pfs_path

    def _onboarding_camera_make(self, cfg: Any) -> str:
        return _camera_make_from_capabilities(self.onboard_detected_capabilities) or cfg.camera.make

    def _onboarding_dimensions(self, cfg: Any, pfs_path: Path) -> tuple[int, int]:
        width_info = self.onboard_detected_capabilities.get("width") or {}
        height_info = self.onboard_detected_capabilities.get("height") or {}
        width_value = _valid_node_value_or_max(width_info)
        height_value = _valid_node_value_or_max(height_info)
        if width_value and height_value:
            return int(width_value), int(height_value)
        pfs_features = parse_pfs_features(pfs_path)
        pfs_width = pfs_features.get("Width")
        pfs_height = pfs_features.get("Height")
        if pfs_width and pfs_height:
            return int(float(pfs_width)), int(float(pfs_height))
        return cfg.camera.expected_width, cfg.camera.expected_height

    def _pfs_frame_rate_mismatch(self, pfs_path: Path, expected_fps: float) -> bool:
        features = parse_pfs_features(pfs_path)
        actual = features.get("AcquisitionFrameRate")
        if actual is None:
            return False
        try:
            return abs(float(actual) - float(expected_fps)) > 0.01
        except (TypeError, ValueError):
            return True

    def _validate_camera_profile_override(
        self,
        base_path: Path,
        camera: dict[str, Any],
        camera_profile: str,
    ) -> None:
        pfs_path = Path(camera_profile).expanduser()
        if not pfs_path.is_absolute():
            pfs_path = (base_path.parent / pfs_path).resolve()
        features = parse_pfs_features(pfs_path)
        comparisons = (
            ("Width", "expected_width", str(camera.get("expected_width"))),
            ("Height", "expected_height", str(camera.get("expected_height"))),
            ("PixelFormat", "expected_pixel_format", str(camera.get("expected_pixel_format"))),
        )
        mismatches = []
        for pfs_key, _config_key, expected in comparisons:
            actual = features.get(pfs_key)
            if actual is not None and actual != expected:
                if pfs_key == "PixelFormat" and bool(camera.get("allow_runtime_pixel_format_override", False)):
                    continue
                mismatches.append(f"{pfs_key} {actual} != {expected}")
        actual_fps = features.get("AcquisitionFrameRate")
        expected_fps = camera.get("expected_fps")
        if actual_fps is not None and expected_fps is not None:
            try:
                fps_matches = abs(float(actual_fps) - float(expected_fps)) <= 0.01
            except (TypeError, ValueError):
                fps_matches = False
            if not fps_matches:
                if not bool(camera.get("allow_runtime_frame_rate_override", False)):
                    mismatches.append(f"AcquisitionFrameRate {actual_fps} != {expected_fps}")
        if mismatches:
            raise ValueError(
                "Selected camera profile does not match the active profile config. "
                "Choose the matching recording profile for this camera instead. "
                "Mismatches: " + "; ".join(mismatches)
            )

    def _metadata_values(self) -> dict[str, Any]:
        values = {field_name: var.get().strip() for field_name, var in self.metadata_vars.items()}
        postnatal_day = _optional_gui_int(values["postnatal_day"], "Postnatal day")
        weight_g = _optional_gui_float(values["weight_g"], "Weight")
        run_index = _optional_gui_int(values["run_index"], "Run index")
        if run_index is None:
            raise ValueError("Run index is required.")
        custom_text = (
            self.custom_fields_text.get("1.0", tk.END).strip()
            if self.custom_fields_text is not None
            else "{}"
        )
        try:
            custom_fields = json.loads(custom_text or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"Custom fields must be valid JSON: {exc.msg}") from exc
        if not isinstance(custom_fields, dict):
            raise ValueError("Custom fields JSON must be an object.")
        custom_fields = normalize_custom_fields(custom_fields)
        postnatal_day_source = "manual"
        if values["date_of_birth"] and postnatal_day is not None:
            try:
                birth_date = date.fromisoformat(values["date_of_birth"])
            except ValueError:
                pass
            else:
                if (datetime.now(timezone.utc).date() - birth_date).days == postnatal_day:
                    postnatal_day_source = "derived_from_date_of_birth"
        if self.notes_text is not None:
            notes = self.notes_text.get("1.0", tk.END).strip()
        else:
            notes = ""
        return {
            "schema_version": 2,
            "project": {
                "project_id": values["project_id"],
                "protocol_id": values["protocol_id"],
                "assay_id": values["assay_id"],
            },
            "subject": {
                "subject_id": values["subject_id"],
                "species": values["species"],
                "date_of_birth": values["date_of_birth"],
                "postnatal_day": postnatal_day,
                "postnatal_day_source": postnatal_day_source,
                "p0_convention": "birth_date_is_p0",
                "weight_g": weight_g,
                "weight_measured_utc": values["weight_measured_utc"],
                "genotype": values["genotype"],
                "experimental_group": values["experimental_group"],
                "sex": values["sex"],
            },
            "acquisition": {
                "experimenter_id": values["experimenter_id"],
                "run_index": run_index,
            },
            "custom_fields": custom_fields,
            "notes": notes,
        }

    def _missing_metadata_fields(self) -> list[str]:
        values = {field_name: var.get().strip() for field_name, var in self.metadata_vars.items()}
        return [
            field_name
            for field_name in REQUIRED_METADATA_FIELDS
            if values.get(field_name, "").strip().upper() in {"", "UNSPECIFIED"}
        ]

    def _duration_s(self) -> float:
        value = float(self.duration_var.get())
        if value <= 0:
            raise ValueError("Duration must be positive.")
        return value

    def _segment_seconds(self, fallback: float | None = None) -> float:
        text = self.segment_seconds_var.get().strip()
        if not text:
            if fallback is not None:
                return float(fallback)
            cfg = load_config(
                self.config_var.get(),
                duration_s=self._duration_s(),
                output_root=self.output_root_var.get(),
            )
            return float(cfg.writer.segment_seconds)
        value = float(text)
        if value <= 0:
            raise ValueError("Segment size must be positive.")
        return value

    def _preview_width(self) -> int:
        value = int(float(self.preview_width_var.get()))
        if value <= 0:
            raise ValueError("Preview target width must be positive.")
        return value

    def _poll_ui_queue(self) -> None:
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except thread_queue.Empty:
                break
            if kind == "devices":
                self._handle_devices(payload)
            elif kind == "record_line":
                self._handle_record_line(str(payload))
            elif kind == "record_done":
                self._handle_record_done(int(payload))
            elif kind == "preview_line":
                self._handle_setup_preview_line(str(payload))
            elif kind == "preview_done":
                self._handle_setup_preview_done(int(payload))
            elif kind == "validation_line":
                self._handle_validation_line(str(payload))
            elif kind == "validation_done":
                self._handle_validation_done(int(payload))
        self.root.after(100, self._poll_ui_queue)

    def _handle_devices(self, report: dict[str, Any]) -> None:
        devices = report.get("devices") or []
        error = report.get("error") or ""
        if devices:
            first = devices[0]
            self.device_status_var.set(
                f"Detected: {first.get('FriendlyName') or first.get('ModelName')} "
                f"serial {first.get('SerialNumber')}"
            )
        elif error:
            self.device_status_var.set(f"Device check error: {error}")
        else:
            self.device_status_var.set("No Basler camera detected")
        self._set_text(self.device_text, json.dumps(_jsonable(report), indent=2, sort_keys=True) + "\n")

    def _handle_record_line(self, line: str) -> None:
        self._append_output(line)
        self.logger.info("record: %s", line.rstrip())
        if self._handle_record_progress(line):
            return
        match = SESSION_RE.search(line.strip())
        if match:
            self.last_session_dir = Path(match.group(1))
            self.session_var.set(str(self.last_session_dir))
        if "Health WARNING" in line or "health WARNING" in line:
            self.monitor_status_var.set("Health warning")
        elif "Health OK" in line or "health OK" in line:
            self.monitor_status_var.set("Recording")
        elif "Writer queue pressure was high" in line:
            self.monitor_status_var.set("Queue pressure")

    def _handle_record_progress(self, line: str) -> bool:
        match = PROGRESS_RE.search(line.strip())
        if not match:
            return False
        elapsed_s = float(match.group("elapsed"))
        frames = int(match.group("frames"))
        expected = int(match.group("expected"))
        fps = float(match.group("fps"))
        queue_depth = int(match.group("queue"))
        queue_max = int(match.group("queue_max"))
        gaps = int(match.group("gaps"))

        elapsed_text = _format_duration(elapsed_s)
        self.record_progress_elapsed_s = elapsed_s
        self.record_progress_wall_at = time.perf_counter()
        progress_text = f"{frames}/{expected} | {fps:.1f} fps | queue {queue_depth}/{queue_max}"
        if gaps:
            progress_text += f" | gaps {gaps}"
            self.monitor_status_var.set("Frame gap")
        elif self.process is not None and self.monitor_status_var.get() not in {
            "Health warning",
            "Queue pressure",
            "Frame gap",
        }:
            self.monitor_status_var.set("Recording")
        self.elapsed_var.set(elapsed_text)
        self.run_progress_var.set(progress_text)
        self.preview_status_var.set(f"{elapsed_text} | {progress_text}")
        return True

    def _handle_setup_preview_line(self, line: str) -> None:
        self._append_output(line)
        self.logger.info("preview: %s", line.rstrip())

    def _handle_setup_preview_done(self, return_code: int) -> None:
        if self.setup_preview_process is None and self.setup_preview_started_at is None:
            return
        self.setup_preview_process = None
        self.setup_preview_started_at = None
        self.setup_preview_stop_requested = False
        if self.setup_preview_previous_preview_enabled is not None:
            self.preview_enabled_var.set(self.setup_preview_previous_preview_enabled)
            self.setup_preview_previous_preview_enabled = None
        if return_code == 0:
            self.status_var.set("Setup preview stopped")
            self.monitor_status_var.set("Preview stopped")
        else:
            self.status_var.set(f"Preview exited with code {return_code}")
            self.monitor_status_var.set(f"Preview exited {return_code}")
        self._append_output(f"=== Setup preview process exited with code {return_code} ===\n")
        self.logger.info("Setup preview exited with code %s", return_code)
        self._refresh_start_state()

    def _handle_validation_line(self, line: str) -> None:
        self._append_output(line)
        self.logger.info("validation: %s", line.rstrip())
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        case_id = payload.get("case_id", "case")
        status = str(payload.get("qc_status") or "unknown")
        fps = payload.get("observed_fps")
        queue_depth = payload.get("max_queue_depth")
        queue_capacity = payload.get("queue_capacity")
        session_dir = str(payload.get("session_dir") or "").strip()
        if session_dir:
            self.last_session_dir = Path(session_dir)
            self.session_var.set(session_dir)
        self.onboard_status_var.set(
            f"{case_id}: {status} | fps={_format_number(fps)} | "
            f"queue={queue_depth}/{queue_capacity} | acquisition={payload.get('acquisition_pass')} | "
            f"pixel={payload.get('pixel_status', 'not_run')} | "
            f"evidence_ready={payload.get('evidence_ready')} | experiment_ready={payload.get('experiment_ready')}"
        )

    def _handle_validation_done(self, return_code: int) -> None:
        self.validation_process = None
        if return_code == 0:
            self.status_var.set("Validation completed")
            self.onboard_status_var.set(
                "Qualification process completed. Exit code 0 does not mean the profile is approved: "
                "review validation_summary.json for metadata/task-quality gates and the preview-mode-specific "
                "lock recommendation, then create and load a locked config."
            )
        else:
            self.status_var.set(f"Validation exited with code {return_code}")
            self.onboard_status_var.set("Validation sweep did not produce a scientific pass. Review command output and validation_summary.json.")
        self._append_output(f"=== Validation sweep exited with code {return_code} ===\n")
        self.refresh_sessions()

    def _handle_record_done(self, return_code: int) -> None:
        self.stop_button.configure(state=tk.DISABLED)
        self.process = None
        self.record_started_at = None
        self.record_progress_elapsed_s = None
        self.record_progress_wall_at = None
        if return_code == 0:
            self.status_var.set("Recording completed")
            self.monitor_status_var.set("Completed")
        else:
            self.status_var.set(f"Recording exited with code {return_code}")
            self.monitor_status_var.set(f"Exited {return_code}")
        self._append_output(f"=== Recording process exited with code {return_code} ===\n")
        self.logger.info("Recording exited with code %s", return_code)
        self.refresh_sessions()
        self._set_next_run_index()
        self._refresh_start_state()
        if return_code == 0 and self.last_session_dir is not None:
            try:
                report = build_session_report(self.last_session_dir)
            except Exception as exc:
                self.qc_status_var.set("Report unavailable")
                self.qc_status_label.configure(style="Warn.TLabel")
                self._append_output(f"WARNING: Could not build report after recording: {exc}\n")
            else:
                self._display_report(self.last_session_dir, report, append_output=False)

    def _tick_elapsed(self) -> None:
        if self.record_started_at is not None:
            if self.record_progress_elapsed_s is not None:
                self.elapsed_var.set(_format_duration(self._record_elapsed_s()))
        elif self.setup_preview_started_at is not None:
            self.elapsed_var.set(_format_duration(time.perf_counter() - self.setup_preview_started_at))
        self.root.after(500, self._tick_elapsed)

    def _record_elapsed_s(self) -> float:
        if self.record_progress_elapsed_s is not None and self.record_progress_wall_at is not None:
            return self.record_progress_elapsed_s + (time.perf_counter() - self.record_progress_wall_at)
        if self.record_started_at is None:
            return 0.0
        return time.perf_counter() - self.record_started_at

    def _poll_preview_image(self) -> None:
        metadata = self._read_preview_metadata()
        if metadata.get("sink") == "shm":
            self._poll_preview_shared_memory(metadata)
            interval_ms = 50 if self.preview_enabled_var.get() or self.process is not None else 200
            self.root.after(interval_ms, self._poll_preview_image)
            return

        image_path = Path(metadata["image_path"]) if metadata.get("image_path") else PREVIEW_IMAGE_PATH
        if image_path.exists():
            try:
                mtime_ns = image_path.stat().st_mtime_ns
            except OSError:
                mtime_ns = self.preview_mtime_ns
            image_key = f"{image_path}|{mtime_ns}"
            if mtime_ns and image_key != self.preview_image_key:
                try:
                    self.preview_base_photo = tk.PhotoImage(file=str(image_path))
                    self._display_preview_photo()
                    self.preview_mtime_ns = mtime_ns
                    self.preview_image_key = image_key
                    self.preview_metadata = metadata
                    self._update_preview_metadata(metadata)
                except (OSError, tk.TclError):
                    pass
        elif self.preview_enabled_var.get() and self.process is not None:
            self.preview_status_var.set("Waiting for preview frame")
        interval_ms = 50 if self.preview_enabled_var.get() or self.process is not None else 200
        self.root.after(interval_ms, self._poll_preview_image)

    def _poll_preview_shared_memory(self, metadata: dict[str, Any]) -> None:
        shm_name = str(metadata.get("shm_name") or "")
        header_bytes = int(metadata.get("header_bytes") or 4096)
        if not shm_name or header_bytes <= 0:
            return
        try:
            if self.preview_shm_name != shm_name:
                self._close_preview_shm()
                self.preview_shm = shared_memory.SharedMemory(name=shm_name)
                self.preview_shm_name = shm_name
            assert self.preview_shm is not None
            frame_metadata = self._read_shm_frame_metadata(header_bytes)
            sequence = int(frame_metadata.get("sequence") or 0)
            width = int(frame_metadata.get("preview_width") or metadata.get("preview_width") or 0)
            height = int(frame_metadata.get("preview_height") or metadata.get("preview_height") or 0)
            if width <= 0 or height <= 0 or sequence <= 0:
                return
            image_key = f"{shm_name}|{sequence}"
            if image_key == self.preview_image_key:
                return
            image_bytes, image_format, display_width, display_height = self._preview_image_from_shm(
                frame_metadata,
                header_bytes,
                width,
                height,
            )
            magic = "P6" if image_format == "PPM" else "P5"
            header = f"{magic}\n{display_width} {display_height}\n255\n".encode("ascii")
            self.preview_base_photo = self._photo_from_pnm_bytes(header + image_bytes, image_format)
            self.preview_image_key = image_key
            self.preview_metadata = frame_metadata
            self._display_preview_photo()
            self._update_preview_metadata(frame_metadata)
        except Exception as exc:
            self.preview_status_var.set(f"Preview unavailable: {type(exc).__name__}")
            self.logger.debug("Integrated preview frame could not be rendered", exc_info=True)

    def _preview_image_from_shm(
        self,
        metadata: dict[str, Any],
        header_bytes: int,
        preview_width: int,
        preview_height: int,
    ) -> tuple[bytes, str, int, int]:
        assert self.preview_shm is not None
        buffer_format = str(metadata.get("buffer_format") or "gray8_downsampled")
        if buffer_format == "gray8_downsampled":
            start = header_bytes
            return (
                bytes(self.preview_shm.buf[start : start + preview_width * preview_height]),
                "PGM",
                preview_width,
                preview_height,
            )

        np = self._preview_np()
        buffer_width = int(metadata.get("buffer_width") or metadata.get("source_width") or 0)
        buffer_height = int(metadata.get("buffer_height") or metadata.get("source_height") or 0)
        stride = int(metadata.get("downsample_stride") or 1)
        if buffer_width <= 0 or buffer_height <= 0:
            return b"", "PGM", preview_width, preview_height
        if buffer_format in {"raw_rgb8", "raw_bgr8"}:
            image = np.ndarray(
                (buffer_height, buffer_width, 3),
                dtype=np.uint8,
                buffer=self.preview_shm.buf,
                offset=header_bytes,
            )
            display = image[::stride, ::stride, :].copy(order="C")
            if buffer_format == "raw_bgr8":
                display = display[:, :, ::-1].copy(order="C")
            return display.tobytes(), "PPM", int(display.shape[1]), int(display.shape[0])

        image = np.ndarray(
            (buffer_height, buffer_width),
            dtype=np.uint8,
            buffer=self.preview_shm.buf,
            offset=header_bytes,
        )
        pixel_format = str(metadata.get("source_pixel_format") or "")
        if self._preview_should_debayer(pixel_format):
            rgb = self._bayer_to_rgb_preview(image, pixel_format)
            if rgb is not None:
                display = rgb[::stride, ::stride, :].copy(order="C")
                return display.tobytes(), "PPM", int(display.shape[1]), int(display.shape[0])

        gray = image[::stride, ::stride].copy(order="C")
        return gray.tobytes(), "PGM", int(gray.shape[1]), int(gray.shape[0])

    def _preview_np(self) -> Any:
        if self.preview_numpy is None:
            import numpy as np

            self.preview_numpy = np
        return self.preview_numpy

    def _preview_cv(self) -> Any | None:
        if self.preview_cv2 is False:
            return None
        if self.preview_cv2 is None:
            try:
                import cv2
            except ImportError:
                self.preview_cv2 = False
            else:
                try:
                    cv2.setNumThreads(1)
                except Exception:
                    pass
                self.preview_cv2 = cv2
        return None if self.preview_cv2 is False else self.preview_cv2

    def _preview_should_debayer(self, pixel_format: str) -> bool:
        mode = self.preview_display_mode_var.get().strip().lower()
        if mode == "raw":
            return False
        if mode == "color":
            return _is_bayer_pixel_format(pixel_format)
        return _is_bayer_pixel_format(pixel_format)

    def _bayer_to_rgb_preview(self, image: Any, pixel_format: str) -> Any | None:
        cv2 = self._preview_cv()
        if cv2 is not None:
            code_name = _bayer_cv2_code_name(pixel_format)
            code = getattr(cv2, code_name, None)
            if code is not None:
                try:
                    return cv2.cvtColor(image, code)
                except Exception:
                    pass
        return _bayer_to_rgb_nearest(self._preview_np(), image, pixel_format)

    def _read_shm_frame_metadata(self, header_bytes: int) -> dict[str, Any]:
        if self.preview_shm is None:
            return {}
        raw = bytes(self.preview_shm.buf[:header_bytes])
        raw = raw.split(b"\0", 1)[0].strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _photo_from_pnm_bytes(self, data: bytes, image_format: str) -> tk.PhotoImage:
        try:
            return tk.PhotoImage(data=data, format=image_format)
        except tk.TclError:
            try:
                return tk.PhotoImage(data=base64.b64encode(data), format=image_format)
            except tk.TclError:
                suffix = ".ppm" if image_format == "PPM" else ".pgm"
                path = RUNTIME_DIR / f"gui_preview_render_{self.preview_render_slot:02d}{suffix}"
                self.preview_render_slot = (self.preview_render_slot + 1) % 4
                path.write_bytes(data)
                return tk.PhotoImage(file=str(path))

    def _display_preview_photo(self) -> None:
        if self.preview_base_photo is None:
            return
        numerator, denominator = self._preview_scale_ratio(self.preview_base_photo)
        image = self.preview_base_photo
        if numerator > 1:
            image = image.zoom(numerator, numerator)
        if denominator > 1:
            image = image.subsample(denominator, denominator)
        self.preview_photo = image
        self.preview_label.configure(image=self.preview_photo, text="")

    def _rescale_preview_image(self) -> None:
        if self.preview_base_photo is not None:
            self._display_preview_photo()

    def _preview_scale_ratio(self, photo: tk.PhotoImage) -> tuple[int, int]:
        panel_w = max(1, self.preview_frame.winfo_width() - 8)
        panel_h = max(1, self.preview_frame.winfo_height() - 8)
        scale = min(panel_w / max(1, photo.width()), panel_h / max(1, photo.height()), 4.0)
        if scale < 1.0:
            denominator = max(1, int((1.0 / scale) + 0.999))
            return 1, denominator
        fraction = Fraction(scale).limit_denominator(4)
        return max(1, fraction.numerator), max(1, fraction.denominator)

    def _read_preview_metadata(self) -> dict[str, Any]:
        meta_path = PREVIEW_IMAGE_PATH.with_suffix(".json")
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _update_preview_metadata(self, metadata: dict[str, Any] | None = None) -> None:
        if metadata is None:
            metadata = self._read_preview_metadata()
        if not metadata:
            self.preview_status_var.set("Integrated preview")
            return
        elapsed = _format_duration(float(metadata.get("elapsed_s") or 0.0))
        fps = float(metadata.get("approx_fps") or 0.0)
        queue_depth = metadata.get("queue_depth")
        queue_max = metadata.get("queue_max_frames")
        frame_count = metadata.get("frames_grabbed")
        expected = metadata.get("expected_frames")
        self.preview_status_var.set(
            f"{elapsed} | {frame_count}/{expected} | {fps:.1f} fps | queue {queue_depth}/{queue_max}"
        )

    def _clear_preview_image(self) -> None:
        self.preview_mtime_ns = 0
        self.preview_image_key = ""
        self.preview_metadata = {}
        self.preview_base_photo = None
        self.preview_photo = None
        self._close_preview_shm()
        for path in [PREVIEW_IMAGE_PATH, PREVIEW_IMAGE_PATH.with_suffix(".json")]:
            try:
                path.unlink()
            except OSError:
                pass
        for pattern in (
            f"{PREVIEW_IMAGE_PATH.stem}_*{PREVIEW_IMAGE_PATH.suffix}",
            f"{PREVIEW_IMAGE_PATH.stem}_*{PREVIEW_IMAGE_PATH.suffix}.tmp",
            f"{PREVIEW_IMAGE_PATH.stem}.json.tmp",
        ):
            for path in RUNTIME_DIR.glob(pattern):
                try:
                    path.unlink()
                except OSError:
                    pass
        if self.preview_enabled_var.get():
            self.preview_label.configure(
                image="",
                text="Waiting for first preview frame...",
                foreground="#bfbfbf",
                background="#050505",
            )
            self.preview_status_var.set("Waiting")
        else:
            self.preview_label.configure(
                image="",
                text="Preview disabled\nEnable integrated preview in Setup",
                foreground="#bfbfbf",
                background="#050505",
            )
            self.preview_status_var.set("Disabled")

    def _close_preview_shm(self) -> None:
        if self.preview_shm is None:
            self.preview_shm_name = ""
            return
        try:
            self.preview_shm.close()
        except Exception:
            pass
        self.preview_shm = None
        self.preview_shm_name = ""

    def _on_close(self) -> None:
        self.logger.info("PyCamRec GUI closing")
        if self._setup_preview_running():
            self.stop_setup_preview(wait=True)
        if self.process is not None and self.process.poll() is None:
            if not messagebox.askyesno(
                "Recording active",
                "A recording process is still active. Request safe stop and close after it exits?",
            ):
                return
            self.stop_recording()
            self.logger.info("Close requested during active recording; safe stop requested")
            return
        if self.validation_process is not None and self.validation_process.poll() is None:
            if not messagebox.askyesno(
                "Validation active",
                "A validation sweep is still running. Stop it and close the GUI?",
            ):
                return
            self.validation_process.terminate()
            self.logger.info("Close requested during active validation; process terminated")
        self._close_preview_shm()
        logging.shutdown()
        self.root.destroy()

    def _append_output(self, text: str) -> None:
        self.monitor_text.configure(state=tk.NORMAL)
        self.monitor_text.insert(tk.END, text)
        self.monitor_text.see(tk.END)
        self.monitor_text.configure(state=tk.DISABLED)

    @staticmethod
    def _set_text(widget: tk.Text, text: str) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, text)
        widget.configure(state=tk.DISABLED)


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _format_number(value: Any, suffix: str = "") -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "unknown"
    return f"{number:.1f}{suffix}"


def _format_bytes(value: float) -> str:
    if value < 0:
        return "-" + _format_bytes(abs(value))
    gib = value / (1024**3)
    if gib >= 1.0:
        return f"{gib:.1f} GiB"
    mib = value / (1024**2)
    return f"{mib:.1f} MiB"


def _qc_display_text(status: str) -> str:
    if status == "pass":
        return "PASS"
    if status == "fail_realtime":
        return "FAIL REALTIME"
    if status == "fail_frame_integrity":
        return "FAIL FRAME INTEGRITY"
    if status == "fail_finalization":
        return "FAIL FINALIZATION"
    if status == "in_progress":
        return "IN PROGRESS"
    return status.replace("_", " ").upper()


def _optional_gui_int(value: str, label: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be an integer.") from exc


def _optional_gui_float(value: str, label: str) -> float | None:
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be numeric.") from exc


def _disk_estimate_text(cfg: Any) -> str:
    bitrate_mbps = cfg.writer.expected_bitrate_mbps or cfg.recording_profile.expected_bitrate_mbps
    compressed_bytes = None
    if bitrate_mbps:
        compressed_bytes = float(bitrate_mbps) * 1_000_000.0 / 8.0 * cfg.session.duration_s
    raw_bytes = float(cfg.frame_bytes) * float(cfg.expected_total_frames)
    try:
        free_bytes = float(shutil.disk_usage(_disk_probe_path(cfg.session.output_root)).free)
    except OSError:
        free_bytes = None

    parts = []
    if compressed_bytes is not None:
        parts.append(f"expected output {_format_bytes(compressed_bytes)}")
    else:
        parts.append("expected output unknown")
    if free_bytes is not None:
        if compressed_bytes is not None:
            parts.append(f"remaining margin {_format_bytes(free_bytes - compressed_bytes)}")
        parts.append(f"free {_format_bytes(free_bytes)}")
        if bitrate_mbps:
            capacity_s = free_bytes * 8.0 / (float(bitrate_mbps) * 1_000_000.0)
            parts.append(f"about {_format_duration(capacity_s)} at target rate before reserve")
    parts.append(f"raw payload would be {_format_bytes(raw_bytes)}")
    prefix = "Disk estimate: "
    if compressed_bytes is not None and free_bytes is not None and compressed_bytes * 1.20 > free_bytes:
        prefix = "WARNING - insufficient 20% disk reserve: "
    return prefix + " | ".join(parts)


def _disk_probe_path(path: Path) -> Path:
    current = path.expanduser()
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _profile_qualification_text(
    cfg: Any,
    *,
    camera_profile_path: str | None = None,
    segment_seconds: float | None = None,
) -> str:
    approval = cfg.raw.get("approval") if isinstance(cfg.raw, dict) else {}
    approval = approval if isinstance(approval, dict) else {}
    status = str(approval.get("status") or "requires_hardware_validation")
    if not status.startswith("locked"):
        return (
            "CANDIDATE — this YAML defines recording settings but is not an approval. "
            "Qualify it on the Profile qualification tab, create a locked config from the passing summary, "
            "and then select that locked config for experiments."
        )
    configured_evidence = str(approval.get("evidence_fingerprint_sha256") or "")
    configured_profile = str(approval.get("profile_fingerprint_sha256") or "")
    intended_mode = str(approval.get("intended_preview_mode") or "off").strip().lower()
    current_mode = "on" if cfg.preview.enabled else "off"
    validated_duration = approval.get("validated_max_duration_s")
    if not configured_evidence or not configured_profile or intended_mode not in {"on", "off"}:
        return "INVALID CERTIFICATE — required fingerprint or intended-preview fields are missing."
    try:
        duration_covered = validated_duration is not None and cfg.session.duration_s <= float(validated_duration)
    except (TypeError, ValueError):
        duration_covered = False
    mode_covered = intended_mode == current_mode
    camera_config = asdict(cfg.camera)
    if camera_profile_path:
        camera_config["pfs_path"] = str(Path(camera_profile_path).expanduser().resolve())
    writer_config = asdict(cfg.writer)
    if segment_seconds is not None:
        writer_config["segment_seconds"] = float(segment_seconds)
    current_profile = build_profile_fingerprint(
        camera_config=camera_config,
        writer_config=writer_config,
        recording_profile=asdict(cfg.recording_profile),
        preview_config=asdict(cfg.preview),
    )["fingerprint_sha256"]
    settings_covered = configured_profile == current_profile
    if not mode_covered or not duration_covered or not settings_covered:
        limits = f"preview {intended_mode}, maximum {validated_duration}s"
        mismatch = " Resolved camera/writer/preview settings differ from the certificate." if not settings_covered else ""
        return (
            f"NOT COVERED BY CERTIFICATE — locked evidence is for {limits}; current setup is "
            f"preview {current_mode}, {cfg.session.duration_s:g}s.{mismatch}"
        )
    return (
        f"LOCK CERTIFICATE PRESENT — covers preview {intended_mode} through {float(validated_duration):g}s. "
        "Camera/PFS/GPU/driver/host/software fingerprints are verified after the camera opens."
    )


def _preview_warning_text(cfg: Any) -> str:
    if not cfg.preview.enabled:
        return ""
    approval = cfg.raw.get("approval") if isinstance(cfg.raw, dict) else {}
    approval = approval if isinstance(approval, dict) else {}
    status = str(approval.get("status") or "")
    intended_mode = str(approval.get("intended_preview_mode") or "off").strip().lower()
    if status.startswith("locked") and intended_mode != "on":
        return (
            "Preview is ON, but the selected lock certificate covers preview OFF. "
            "Turn preview off or load/produce a preview-on locked config."
        )
    if not status.startswith("locked") and cfg.recording_profile.id in SCIENTIFIC_PREVIEW_PROFILE_IDS:
        return (
            "Preview is ON for an unapproved candidate profile. This is allowed for engineering tests, "
            "but scientific use requires preview-on qualification and a matching locked config."
        )
    if str(cfg.camera.expected_pixel_format).upper() in {"RGB8", "BGR8"}:
        return "RGB/BGR camera output triples the USB payload; run the validation sweep before scientific color recording."
    return ""


def _preferred_pixel_format(pixel_formats: list[str]) -> str:
    for candidate in ("Mono8", "BayerBG8", "BayerRG8", "BayerGB8", "BayerGR8", "RGB8", "BGR8"):
        if candidate in pixel_formats:
            return candidate
    return pixel_formats[0] if pixel_formats else "Mono8"


def _integrated_preview_sink(*, setup_preview: bool, pixel_format: str) -> str:
    """Use the lightest shared-memory payload that preserves preview semantics."""

    if setup_preview and not is_mono_pixel_format(pixel_format):
        return "shm_raw"
    return "shm"


def _matching_pfs_for_camera(serial: str, pixel_format: str) -> Path | None:
    serial = str(serial or "").strip()
    if not serial:
        return None
    search_roots = {WORKSPACE_ROOT, RESOURCE_ROOT}
    candidates = sorted(
        {
            path.resolve()
            for root in search_roots
            for path in root.glob(f"*{serial}*.pfs")
        }
    )
    if not candidates:
        return None
    scored = [(_pfs_candidate_score(path, pixel_format), len(path.name), str(path).lower(), path) for path in candidates]
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return scored[0][3]


def _pfs_candidate_score(path: Path, pixel_format: str) -> int:
    score = 0
    name = path.name.lower()
    try:
        features = parse_pfs_features(path)
    except OSError:
        features = {}
    pfs_pixel = str(features.get("PixelFormat") or "")
    if pfs_pixel == pixel_format:
        score += 50
    if is_mono_pixel_format(pixel_format):
        if "mono" in name:
            score += 30
        if is_mono_pixel_format(pfs_pixel):
            score += 30
        elif pfs_pixel:
            score -= 25
    elif is_bayer_camera_pixel_format(pixel_format):
        if is_bayer_camera_pixel_format(pfs_pixel):
            score += 30
        if "mono" in name:
            score -= 20
    elif is_rgb_pixel_format(pixel_format):
        if is_rgb_pixel_format(pfs_pixel):
            score += 35
        elif is_bayer_camera_pixel_format(pfs_pixel):
            score += 25
        if "mono" in name or is_mono_pixel_format(pfs_pixel):
            score -= 25
        else:
            score += 10
    return score


def _camera_make_from_capabilities(report: dict[str, Any]) -> str:
    device_info = report.get("device_info") or {}
    tokens = " ".join(str(device_info.get(key) or "") for key in ("DeviceClass", "TLType", "DeviceFactory"))
    normalized = tokens.lower()
    if "usb" in normalized or "u3v" in normalized:
        return "basler_usb"
    if "cxp" in normalized:
        return "basler_cxp"
    return "basler" if report else ""


def _suggested_onboarding_fps(report: dict[str, Any], pixel_format: str) -> float | None:
    fps_info = report.get("acquisition_frame_rate") or {}
    value = fps_info.get("value")
    if value is None:
        max_value = fps_info.get("max")
        if max_value is not None and float(max_value) < 10_000:
            value = max_value
    if value is None:
        return None
    suggested = float(value)
    model = str(report.get("model") or (report.get("device_info") or {}).get("ModelName") or "").lower()
    if is_rgb_pixel_format(pixel_format) and "aca1300" in model and suggested > 75.0:
        return 75.0
    return suggested


def _valid_node_value_or_max(info: dict[str, Any]) -> int | None:
    safe_value = _numeric_or_none(info.get("safe_value"))
    if safe_value is not None:
        return int(safe_value)
    value = _numeric_or_none(info.get("value"))
    minimum = _numeric_or_none(info.get("min"))
    maximum = _numeric_or_none(info.get("max"))
    if value is not None:
        above_min = minimum is None or value >= minimum
        below_max = maximum is None or value <= maximum
        if above_min and below_max:
            return int(value)
    if maximum is not None:
        return int(maximum)
    return int(value) if value is not None else None


def _numeric_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _make_app_logger() -> tuple[logging.Logger, Path]:
    log_dir = RUNTIME_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"pycamrec_gui_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logger = logging.getLogger(f"pycamrec.gui.{id(log_path)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger, log_path


def _open_path(path: Path) -> None:
    path = path.expanduser().resolve()
    if not path.exists():
        messagebox.showerror("Path not found", str(path))
        return
    os.startfile(str(path))


def _run_pycamrec_json(args: list[str], timeout_s: float) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "pycamrec", *args],
        cwd=str(WORKSPACE_ROOT),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    output = (completed.stdout or "").strip()
    error_output = (completed.stderr or "").strip()
    if completed.returncode != 0:
        try:
            data = _parse_json_from_text(output)
        except ValueError:
            data = {}
        data["error"] = data.get("error") or error_output or output or f"exit code {completed.returncode}"
        return data
    return _parse_json_from_text(output)


def _parse_json_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        return {}
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end < start:
            raise ValueError(stripped)
        data = json.loads(stripped[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("PyCamRec command did not return a JSON object.")
    return data


def _device_report_has_serial(report: dict[str, Any], serial: str) -> bool:
    for device in report.get("devices") or []:
        if str(device.get("SerialNumber") or "") == str(serial):
            return True
    for interface in report.get("interfaces") or []:
        for device in interface.get("Devices") or []:
            if str(device.get("SerialNumber") or "") == str(serial):
                return True
    return False


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(WORKSPACE_ROOT)
        if not existing
        else str(WORKSPACE_ROOT) + os.pathsep + existing
    )
    if os.name == "nt":
        basler_x64 = r"C:\Program Files\Basler\pylon 7\Runtime\x64"
        basler_cxp = r"C:\Program Files\Basler\pylon 7\Runtime\x64\pylonCXP\bin"
        env["GENICAM_GENTL64_PATH"] = _prepend_env_paths(
            env.get("GENICAM_GENTL64_PATH", ""),
            [basler_x64, basler_cxp],
        )
        env["PATH"] = _prepend_env_paths(env.get("PATH", ""), [basler_x64, basler_cxp])
    return env


def _prepend_env_paths(current: str, paths: list[str]) -> str:
    existing = [item for item in current.split(os.pathsep) if item]
    normalized_existing = {item.rstrip("\\/").lower() for item in existing}
    additions = [
        item
        for item in paths
        if Path(item).exists() and item.rstrip("\\/").lower() not in normalized_existing
    ]
    return os.pathsep.join(additions + existing)


def _is_bayer_pixel_format(pixel_format: str) -> bool:
    return _bayer_pattern(pixel_format) is not None


def _bayer_pattern(pixel_format: str) -> str | None:
    normalized = pixel_format.replace("_", "").replace(" ", "").lower()
    for pattern in ("bg", "gb", "gr", "rg"):
        if normalized.startswith(f"bayer{pattern}"):
            return pattern
    if normalized.startswith("bayer8"):
        return "bg"
    return None


def _bayer_cv2_code_name(pixel_format: str) -> str:
    pattern = _bayer_pattern(pixel_format) or "bg"
    return {
        "bg": "COLOR_BayerBG2RGB",
        "gb": "COLOR_BayerGB2RGB",
        "gr": "COLOR_BayerGR2RGB",
        "rg": "COLOR_BayerRG2RGB",
    }[pattern]


def _bayer_to_rgb_nearest(np: Any, image: Any, pixel_format: str) -> Any | None:
    pattern = _bayer_pattern(pixel_format)
    if pattern is None or image.shape[0] < 2 or image.shape[1] < 2:
        return None
    even_height = image.shape[0] - (image.shape[0] % 2)
    even_width = image.shape[1] - (image.shape[1] % 2)
    cells = image[:even_height, :even_width].reshape(even_height // 2, 2, even_width // 2, 2)
    rgb = np.empty((even_height // 2, even_width // 2, 3), dtype=np.uint8)
    if pattern == "bg":
        blue = cells[:, 0, :, 0]
        green_a = cells[:, 0, :, 1]
        green_b = cells[:, 1, :, 0]
        red = cells[:, 1, :, 1]
    elif pattern == "gb":
        green_a = cells[:, 0, :, 0]
        blue = cells[:, 0, :, 1]
        red = cells[:, 1, :, 0]
        green_b = cells[:, 1, :, 1]
    elif pattern == "gr":
        green_a = cells[:, 0, :, 0]
        red = cells[:, 0, :, 1]
        blue = cells[:, 1, :, 0]
        green_b = cells[:, 1, :, 1]
    else:
        red = cells[:, 0, :, 0]
        green_a = cells[:, 0, :, 1]
        green_b = cells[:, 1, :, 0]
        blue = cells[:, 1, :, 1]
    rgb[:, :, 0] = red
    rgb[:, :, 1] = ((green_a.astype(np.uint16) + green_b.astype(np.uint16)) // 2).astype(np.uint8)
    rgb[:, :, 2] = blue
    return np.repeat(np.repeat(rgb, 2, axis=0), 2, axis=1)


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


def main() -> int:
    root = tk.Tk()
    PyCamRecApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
