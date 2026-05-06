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
import logging
from dataclasses import asdict
from datetime import datetime
from fractions import Fraction
from multiprocessing import shared_memory
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from . import __release_stage__, __version__
from .config import load_config
from .preflight import run_preflight
from .report import build_session_report


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = WORKSPACE_ROOT / "configs"
DEFAULT_OUTPUT_ROOT = Path("D:/PyCamRecSessions")
RUNTIME_DIR = WORKSPACE_ROOT / ".pycamrec_gui"
PREVIEW_IMAGE_PATH = RUNTIME_DIR / "latest_preview.pgm"

PROFILE_CONFIGS = (
    CONFIG_DIR / "pycamrec_basler_a2A2448_profile_long_lossy.yaml",
    CONFIG_DIR / "pycamrec_basler_a2A2448_profile_near_lossless.yaml",
    CONFIG_DIR / "pycamrec_basler_a2A2448_profile_lossless.yaml",
)
PROFILE_CHOICES = (
    ("Long", PROFILE_CONFIGS[0]),
    ("Near-lossless", PROFILE_CONFIGS[1]),
    ("Lossless calibration", PROFILE_CONFIGS[2]),
)
PROFILE_PATH_BY_LABEL = {label: path for label, path in PROFILE_CHOICES}
PROFILE_LABELS = tuple(label for label, _path in PROFILE_CHOICES)
CUSTOM_PROFILE_LABEL = "Custom"
SCIENTIFIC_PREVIEW_PROFILE_IDS = {"long_lossy_h264_250m", "near_lossless_h264_400m"}

METADATA_FIELDS = (
    ("animal_id", "Animal ID"),
    ("dob", "DOB"),
    ("test_assay_name", "Test / assay name"),
    ("genotype", "Genotype"),
    ("experimental_group", "Experimental group"),
    ("sex", "Sex"),
    ("experimentator", "Experimentator"),
    ("project_protocol", "Project / protocol"),
    ("camera_profile_path", "Camera profile path"),
)

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


class PyCamRecApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"PyCamRec {__version__} ({__release_stage__})")
        self.root.geometry("1320x820")
        self.root.minsize(1100, 680)

        self.ui_queue: thread_queue.Queue[tuple[str, Any]] = thread_queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.setup_preview_process: subprocess.Popen[str] | None = None
        self.reader_thread: threading.Thread | None = None
        self.setup_preview_reader_thread: threading.Thread | None = None
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

        self.profile_choice_var = tk.StringVar(value=PROFILE_LABELS[0])
        self.config_var = tk.StringVar(value=str(PROFILE_CONFIGS[0]))
        self.camera_profile_var = tk.StringVar(value="")
        self.output_root_var = tk.StringVar(value=str(DEFAULT_OUTPUT_ROOT))
        self.duration_var = tk.StringVar(value="60")
        self.preview_enabled_var = tk.BooleanVar(value=False)
        self.preview_width_var = tk.StringVar(value="512")
        self.preview_fps_var = tk.StringVar(value="20")
        self.allow_unspecified_var = tk.BooleanVar(value=False)

        self.status_var = tk.StringVar(value="Idle")
        self.device_status_var = tk.StringVar(value="Device status not checked")
        self.monitor_status_var = tk.StringVar(value="Idle")
        self.elapsed_var = tk.StringVar(value="00:00")
        self.run_progress_var = tk.StringVar(value="Frames -- | FPS -- | Queue --")
        self.session_var = tk.StringVar(value="")
        self.qc_status_var = tk.StringVar(value="Not run")
        self.profile_summary_var = tk.StringVar(value="")
        self.disk_estimate_var = tk.StringVar(value="Disk estimate pending")
        self.preview_warning_var = tk.StringVar(value="")
        self.preview_status_var = tk.StringVar(value="Preview panel ready")
        self.metadata_status_var = tk.StringVar(value="Fields default to UNSPECIFIED until filled.")

        self.metadata_vars = {
            field_name: tk.StringVar(value="UNSPECIFIED") for field_name, _ in METADATA_FIELDS
        }
        self.notes_text: tk.Text | None = None
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
        ttk.Label(top, text="Camera and CXP transport", style="Header.TLabel").pack(side=tk.LEFT)
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

        ttk.Label(form, text="Output root").grid(row=4, column=0, sticky=tk.W, pady=3)
        ttk.Entry(form, textvariable=self.output_root_var).grid(row=4, column=1, sticky=tk.EW, pady=3, padx=(8, 4))
        ttk.Button(form, text="Browse", command=self.browse_output_root).grid(row=4, column=2, sticky=tk.EW)

        ttk.Label(form, textvariable=self.disk_estimate_var, style="Small.TLabel", wraplength=720).grid(
            row=5,
            column=1,
            columnspan=2,
            sticky=tk.W,
            pady=(0, 4),
            padx=(8, 4),
        )

        preview_frame = ttk.LabelFrame(form, text="Integrated preview")
        preview_frame.grid(row=6, column=0, columnspan=3, sticky=tk.EW, pady=(8, 4))
        ttk.Checkbutton(preview_frame, text="Enable", variable=self.preview_enabled_var).pack(side=tk.LEFT, padx=(8, 8), pady=6)
        ttk.Label(preview_frame, text="Target width").pack(side=tk.LEFT, padx=(8, 4))
        ttk.Entry(preview_frame, textvariable=self.preview_width_var, width=7).pack(side=tk.LEFT)
        ttk.Label(preview_frame, text="FPS").pack(side=tk.LEFT, padx=(12, 4))
        ttk.Entry(preview_frame, textvariable=self.preview_fps_var, width=7).pack(side=tk.LEFT)
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
        ttk.Label(preview_frame, text="512/20 is live-view target; preview-on runs still need QC.").pack(
            side=tk.LEFT,
            padx=(14, 4),
        )

        ttk.Label(form, textvariable=self.preview_warning_var, style="Warning.TLabel", wraplength=720).grid(
            row=7,
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
        ).grid(row=8, column=1, columnspan=2, sticky=tk.W, pady=(8, 3), padx=(8, 4))

        form.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Selected profile", style="Header.TLabel").pack(anchor=tk.W, pady=(14, 4))
        ttk.Label(frame, textvariable=self.profile_summary_var, wraplength=520, justify=tk.LEFT).pack(fill=tk.X)

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
        self.notebook.add(frame, text="Metadata")

        ttk.Label(frame, text="Experiment metadata", style="Header.TLabel").grid(
            row=0,
            column=0,
            columnspan=3,
            sticky=tk.W,
            pady=(0, 8),
        )
        for row, (field_name, label) in enumerate(METADATA_FIELDS, start=1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky=tk.W, pady=3)
            ttk.Entry(frame, textvariable=self.metadata_vars[field_name]).grid(
                row=row,
                column=1,
                sticky=tk.EW,
                pady=3,
                padx=(8, 4),
            )
            if field_name == "camera_profile_path":
                ttk.Button(frame, text="Use PFS", command=self.use_selected_config_as_camera_profile).grid(
                    row=row,
                    column=2,
                    sticky=tk.EW,
                )

        ttk.Label(frame, text="Notes").grid(row=10, column=0, sticky=tk.NW, pady=3)
        self.notes_text = tk.Text(frame, height=6, wrap=tk.WORD)
        self.notes_text.grid(row=10, column=1, columnspan=2, sticky=tk.NSEW, pady=3, padx=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=11, column=1, columnspan=2, sticky=tk.W, pady=(10, 0), padx=(8, 0))
        ttk.Button(buttons, text="Check metadata", command=self.check_metadata).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Reset", command=self.reset_metadata).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(frame, textvariable=self.metadata_status_var, wraplength=500).grid(
            row=12,
            column=1,
            columnspan=2,
            sticky=tk.W,
            pady=(8, 0),
            padx=(8, 0),
        )

        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(10, weight=1)

    def _build_reports_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Reports")

        top = ttk.Frame(frame)
        top.pack(fill=tk.X)
        ttk.Button(top, text="Refresh", command=self.refresh_sessions).pack(side=tk.LEFT)
        ttk.Button(top, text="Generate report", command=self.generate_selected_report).pack(side=tk.LEFT, padx=(8, 0))
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
            self.output_root_var,
            self.config_var,
            self.camera_profile_var,
            self.preview_width_var,
            self.preview_fps_var,
        ):
            var.trace_add("write", lambda *_args: self.root.after_idle(self._load_profile_summary))
        self.preview_enabled_var.trace_add("write", lambda *_args: self.root.after_idle(self._load_profile_summary))
        self.allow_unspecified_var.trace_add("write", lambda *_args: self.root.after_idle(self._refresh_start_state))
        for var in self.metadata_vars.values():
            var.trace_add("write", lambda *_args: self.root.after_idle(self._refresh_start_state))

    def _on_profile_choice(self) -> None:
        path = PROFILE_PATH_BY_LABEL.get(self.profile_choice_var.get())
        if path is not None:
            self.config_var.set(str(path))
            self.camera_profile_var.set("")
        self._load_profile_summary()

    def _refresh_start_state(self) -> None:
        process_running = self.process is not None and self.process.poll() is None
        setup_preview_running = self._setup_preview_running()
        missing = self._missing_metadata_fields()
        metadata_ok = not missing or self.allow_unspecified_var.get()
        state = tk.NORMAL if metadata_ok and not process_running and not setup_preview_running else tk.DISABLED
        for button in self.start_buttons:
            button.configure(state=state)
        preview_start_state = tk.DISABLED if process_running or setup_preview_running else tk.NORMAL
        preview_stop_state = tk.NORMAL if setup_preview_running else tk.DISABLED
        for button in self.preview_start_buttons:
            button.configure(state=preview_start_state)
        for button in self.preview_stop_buttons:
            button.configure(state=preview_stop_state)
        if process_running:
            return
        if setup_preview_running:
            self.metadata_status_var.set("Setup preview is active. Stop preview before recording.")
            return
        if missing and self.allow_unspecified_var.get():
            self.metadata_status_var.set("Engineering mode: metadata incomplete but recording is allowed.")
        elif missing:
            self.metadata_status_var.set("Start disabled until metadata is complete: " + ", ".join(missing))
        else:
            self.metadata_status_var.set("Metadata complete.")

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

    def browse_output_root(self) -> None:
        path = filedialog.askdirectory(initialdir=self.output_root_var.get() or str(DEFAULT_OUTPUT_ROOT))
        if path:
            self.output_root_var.set(path)
            self.refresh_sessions()
            self._load_profile_summary()

    def use_selected_config_as_camera_profile(self) -> None:
        camera_profile = self.camera_profile_var.get().strip()
        if camera_profile:
            self.metadata_vars["camera_profile_path"].set(str(Path(camera_profile).expanduser().resolve()))
        else:
            self.metadata_vars["camera_profile_path"].set(str(Path(self.config_var.get()).resolve()))
        self.check_metadata()

    def reset_metadata(self) -> None:
        for field_name, _ in METADATA_FIELDS:
            self.metadata_vars[field_name].set("UNSPECIFIED")
        if self.notes_text is not None:
            self.notes_text.delete("1.0", tk.END)
        self.metadata_status_var.set("Fields reset to UNSPECIFIED.")
        self._refresh_start_state()

    def check_metadata(self) -> bool:
        missing = self._missing_metadata_fields()
        if missing:
            self.metadata_status_var.set("Missing: " + ", ".join(missing))
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
        else:
            self.status_var.set("Preflight OK")

    def start_setup_preview(self) -> None:
        if self._setup_preview_running():
            messagebox.showinfo("Preview active", "Setup preview is already running.")
            return
        if self.process is not None and self.process.poll() is None:
            messagebox.showinfo("Recording active", "Stop recording before starting setup preview.")
            return

        try:
            runtime_config = self._write_runtime_config(preview_enabled_override=True)
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

    def refresh_sessions(self) -> None:
        if not hasattr(self, "session_list"):
            return
        root = Path(self.output_root_var.get()).expanduser()
        self.session_list.delete(0, tk.END)
        if not root.exists():
            return
        sessions = sorted(
            [path for path in root.iterdir() if path.is_dir()],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
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

    def _show_report(self, session_dir: Path) -> None:
        try:
            report = build_session_report(session_dir)
        except Exception as exc:
            messagebox.showerror("Report failed", str(exc))
            return
        self._display_report(session_dir, report, append_output=True)
        self.notebook.select(3)
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
            self.disk_estimate_var.set("Disk estimate unavailable until the profile loads.")
            self.preview_warning_var.set("")
            self._refresh_start_state()
            return
        if not self.camera_profile_var.get().strip():
            self.camera_profile_var.set(str(cfg.camera.pfs_path))
        profile = cfg.recording_profile
        self.profile_summary_var.set(
            f"{profile.display_name} | {profile.pixel_fidelity} | "
            f"{profile.validation_status} | expected {profile.expected_bitrate_mbps} Mbps\n"
            f"{profile.recommended_use}"
        )
        self.disk_estimate_var.set(_disk_estimate_text(cfg))
        self.preview_warning_var.set(_preview_warning_text(cfg))
        self._refresh_start_state()

    def _write_runtime_config(self, preview_enabled_override: bool | None = None) -> Path:
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

        camera_profile = self.camera_profile_var.get().strip()
        if camera_profile:
            camera = dict(data.get("camera", {}))
            camera["pfs_path"] = camera_profile
            data["camera"] = camera

        preview = dict(data.get("preview", {}))
        preview["enabled"] = bool(self.preview_enabled_var.get() if preview_enabled_override is None else preview_enabled_override)
        preview["width"] = self._preview_width()
        preview["max_fps"] = float(self.preview_fps_var.get())
        preview["sink"] = "shm_raw"
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

    def _metadata_values(self) -> dict[str, str]:
        values = {field_name: var.get().strip() for field_name, var in self.metadata_vars.items()}
        if self.notes_text is not None:
            values["notes"] = self.notes_text.get("1.0", tk.END).strip()
        else:
            values["notes"] = ""
        return values

    def _missing_metadata_fields(self) -> list[str]:
        values = self._metadata_values()
        return [
            field_name
            for field_name, _ in METADATA_FIELDS
            if values.get(field_name, "").strip() in {"", "UNSPECIFIED"}
        ]

    def _duration_s(self) -> float:
        value = float(self.duration_var.get())
        if value <= 0:
            raise ValueError("Duration must be positive.")
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
        if "health WARNING" in line:
            self.monitor_status_var.set("Health warning")
        elif "health OK" in line:
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
            payload = self._preview_payload_from_shm(frame_metadata, header_bytes, width, height)
            header = f"P5\n{width} {height}\n255\n".encode("ascii")
            self.preview_base_photo = self._photo_from_pgm_bytes(header + payload)
            self.preview_image_key = image_key
            self.preview_metadata = frame_metadata
            self._display_preview_photo()
            self._update_preview_metadata(frame_metadata)
        except Exception as exc:
            self.preview_status_var.set(f"Preview unavailable: {type(exc).__name__}")

    def _preview_payload_from_shm(
        self,
        metadata: dict[str, Any],
        header_bytes: int,
        preview_width: int,
        preview_height: int,
    ) -> bytes:
        assert self.preview_shm is not None
        buffer_format = str(metadata.get("buffer_format") or "gray8_downsampled")
        if buffer_format != "raw_mono8":
            start = header_bytes
            return bytes(self.preview_shm.buf[start : start + preview_width * preview_height])

        np = self._preview_np()
        buffer_width = int(metadata.get("buffer_width") or metadata.get("source_width") or 0)
        buffer_height = int(metadata.get("buffer_height") or metadata.get("source_height") or 0)
        stride = int(metadata.get("downsample_stride") or 1)
        if buffer_width <= 0 or buffer_height <= 0:
            return b""
        image = np.ndarray(
            (buffer_height, buffer_width),
            dtype=np.uint8,
            buffer=self.preview_shm.buf,
            offset=header_bytes,
        )
        return image[::stride, ::stride].copy(order="C").tobytes()

    def _preview_np(self) -> Any:
        if self.preview_numpy is None:
            import numpy as np

            self.preview_numpy = np
        return self.preview_numpy

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

    def _photo_from_pgm_bytes(self, data: bytes) -> tk.PhotoImage:
        try:
            return tk.PhotoImage(data=data, format="PGM")
        except tk.TclError:
            try:
                return tk.PhotoImage(data=base64.b64encode(data), format="PGM")
            except tk.TclError:
                path = RUNTIME_DIR / f"gui_preview_render_{self.preview_render_slot:02d}.pgm"
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
    return status.replace("_", " ").upper()


def _disk_estimate_text(cfg: Any) -> str:
    bitrate_mbps = cfg.recording_profile.expected_bitrate_mbps or cfg.writer.expected_bitrate_mbps
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
    parts.append(f"raw payload would be {_format_bytes(raw_bytes)}")
    return "Disk estimate: " + " | ".join(parts)


def _disk_probe_path(path: Path) -> Path:
    current = path.expanduser()
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _preview_warning_text(cfg: Any) -> str:
    if not cfg.preview.enabled:
        return ""
    if cfg.recording_profile.id in SCIENTIFIC_PREVIEW_PROFILE_IDS:
        return (
            "Preview is enabled for a scientific recording profile. Use it for positioning, "
            "then validate important runs with preview off if QC reports host-timing pressure."
        )
    if cfg.recording_profile.pixel_fidelity == "lossless":
        return "Preview is enabled during lossless calibration; keep runs short and confirm QC after recording."
    return ""


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
