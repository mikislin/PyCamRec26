"""Print a compact validation sweep summary from cmd.exe or PowerShell."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Review a PyCamRec validation_sweep.csv without PowerShell-only commands."
    )
    parser.add_argument("path", type=Path, help="Sweep folder or validation_sweep.csv path.")
    parser.add_argument("--passes-only", action="store_true", help="Only show approved experiment-ready passes.")
    parser.add_argument("--evidence-ready-only", action="store_true", help="Only show profile-lock evidence candidates.")
    parser.add_argument(
        "--failures-only",
        action="store_true",
        help="Only show acquisition/QC failures (experiment readiness is reported separately).",
    )
    args = parser.parse_args()

    csv_path = args.path / "validation_sweep.csv" if args.path.is_dir() else args.path
    if not csv_path.is_file():
        raise SystemExit(f"validation_sweep.csv not found: {csv_path}")

    rows = list(csv.DictReader(csv_path.open(newline="", encoding="utf-8")))
    if args.passes_only:
        rows = [row for row in rows if _truthy(row.get("scientific_pass"))]
    if args.failures_only:
        rows = [row for row in rows if not _truthy(row.get("qc_pass"))]
    if args.evidence_ready_only:
        rows = [row for row in rows if _truthy(row.get("evidence_ready"))]

    rows.sort(
        key=lambda row: (
            _float(row.get("duration_s")),
            _float(row.get("bitrate_mbps")),
            row.get("preview_enabled", ""),
            row.get("case_id", ""),
        )
    )
    _print_table(rows)
    return 0


def _print_table(rows: list[dict[str, str]]) -> None:
    columns = [
        ("case_id", "case"),
        ("acquisition_pass", "acq"),
        ("qc_pass", "qcpass"),
        ("evidence_ready", "evidence"),
        ("experiment_ready", "ready"),
        ("qc_status", "qc"),
        ("duration_s", "sec"),
        ("preview_enabled", "preview"),
        ("observed_fps", "fps"),
        ("max_queue_depth", "qmax"),
        ("queue_capacity", "qcap"),
        ("queue_fraction", "q%"),
        ("pixel_status", "pixel"),
        ("rollover_pass", "roll"),
        ("segment_mbps", "mbps"),
        ("ffprobe_frames", "probe"),
    ]
    formatted = []
    for row in rows:
        qcap = _float(row.get("queue_capacity"))
        qmax = _float(row.get("max_queue_depth"))
        qtext = _fmt_int(row.get("max_queue_depth"))
        if qcap > 0 and qmax >= 0:
            qtext = f"{int(qmax)}/{int(qcap)}"
        formatted.append(
            {
                "case": row.get("case_id", ""),
                "acq": "yes" if _truthy(row.get("acquisition_pass")) else "no",
                "qcpass": "yes" if _truthy(row.get("qc_pass")) else "no",
                "evidence": "yes" if _truthy(row.get("evidence_ready")) else "no",
                "ready": "yes" if _truthy(row.get("experiment_ready")) else "no",
                "qc": row.get("qc_status", ""),
                "sec": _fmt_num(row.get("duration_s")),
                "preview": "on" if _truthy(row.get("preview_enabled")) else "off",
                "fps": _fmt_num(row.get("observed_fps"), digits=3),
                "qmax": qtext,
                "qcap": _fmt_int(row.get("queue_capacity")),
                "q%": _fmt_percent(row.get("queue_fraction")),
                "pixel": row.get("pixel_status", ""),
                "roll": "yes" if _truthy(row.get("rollover_pass")) else "no",
                "mbps": _fmt_num(row.get("segment_mbps"), digits=1),
                "probe": _fmt_int(row.get("ffprobe_frames")),
            }
        )
    if not formatted:
        print("No rows matched.")
        return
    headers = [header for _, header in columns]
    widths = {
        header: max(len(header), *(len(row.get(header, "")) for row in formatted))
        for header in headers
    }
    print("  ".join(header.ljust(widths[header]) for header in headers))
    print("  ".join("-" * widths[header] for header in headers))
    for row in formatted:
        print("  ".join(row.get(header, "").ljust(widths[header]) for header in headers))


def _truthy(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _float(value: str | None) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return -1.0


def _fmt_num(value: str | None, *, digits: int = 0) -> str:
    number = _float(value)
    if number < 0:
        return ""
    return f"{number:.{digits}f}"


def _fmt_int(value: str | None) -> str:
    number = _float(value)
    if number < 0:
        return ""
    return str(int(number))


def _fmt_percent(value: str | None) -> str:
    number = _float(value)
    if number < 0:
        return ""
    return f"{number * 100:.1f}"


if __name__ == "__main__":
    raise SystemExit(main())
