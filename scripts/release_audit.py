"""Fail when the public release surface differs from release-files.txt."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path, PurePosixPath


FORBIDDEN_SUFFIXES = {
    ".avi",
    ".framemd5",
    ".h264",
    ".log",
    ".mp4",
    ".part",
    ".pgm",
    ".ppm",
    ".pyc",
    ".raw",
}
FORBIDDEN_PARTS = {
    ".pycamrec_gui",
    ".pytest_cache",
    "__pycache__",
    "build",
    "configs_legacy",
    "dist",
    "generated",
    "approved",
    "pycamrec.egg-info",
    "ssd_sessions",
    "validation_sweeps",
}
SECRET_NAMES = {
    ".env",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
    "secrets.json",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that Git sees exactly the reviewed PyCamRec release files."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Release manifest; defaults to release-files.txt at the repository root.",
    )
    parser.add_argument(
        "--max-file-mib",
        type=float,
        default=5.0,
        help="Reject unexpectedly large public files (default: 5 MiB).",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    manifest_path = (args.manifest or root / "release-files.txt").resolve()
    errors: list[str] = []
    try:
        manifest = _read_manifest(manifest_path, root)
    except (OSError, ValueError) as exc:
        print(f"RELEASE AUDIT FAILED\n- {exc}")
        return 1

    candidates = set(_git_release_candidates(root))
    declared = set(manifest)
    for path in sorted(candidates - declared):
        errors.append(f"unreviewed public candidate: {path}")
    for path in sorted(declared - candidates):
        errors.append(f"manifest entry is missing or ignored: {path}")

    max_bytes = int(args.max_file_mib * 1024 * 1024)
    for relative in manifest:
        posix = PurePosixPath(relative)
        parts = {part.casefold() for part in posix.parts}
        if parts & FORBIDDEN_PARTS:
            errors.append(f"runtime/generated directory is forbidden: {relative}")
        if posix.suffix.casefold() in FORBIDDEN_SUFFIXES or ".part." in posix.name.casefold():
            errors.append(f"recording/runtime suffix is forbidden: {relative}")
        if posix.name.casefold() in SECRET_NAMES:
            errors.append(f"secret-bearing filename is forbidden: {relative}")
        path = root / Path(*posix.parts)
        if path.is_file() and path.stat().st_size > max_bytes:
            errors.append(
                f"file exceeds {args.max_file_mib:g} MiB review limit: {relative} "
                f"({path.stat().st_size / 1024 / 1024:.2f} MiB)"
            )

    if errors:
        print("RELEASE AUDIT FAILED")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"RELEASE AUDIT PASS: {len(manifest)} reviewed files; no release-surface drift.")
    return 0


def _read_manifest(path: Path, root: Path) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        normalized = PurePosixPath(value).as_posix()
        if PurePosixPath(normalized).is_absolute() or ".." in PurePosixPath(normalized).parts:
            raise ValueError(f"unsafe manifest path on line {line_number}: {value}")
        if normalized in seen:
            raise ValueError(f"duplicate manifest path on line {line_number}: {normalized}")
        if not (root / Path(*PurePosixPath(normalized).parts)).is_file():
            raise ValueError(f"manifest file does not exist: {normalized}")
        seen.add(normalized)
        paths.append(normalized)
    return paths


def _git_release_candidates(root: Path) -> list[str]:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "safe.directory=*",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip().replace("\\", "/") for line in completed.stdout.splitlines() if line.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
