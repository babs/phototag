"""XMP sidecar writer (exiftool-based).

Writes a `<image>.xmp` file next to the original image. The original is
never touched — the sidecar carries `dc:Subject` (flat keywords) and
optionally `lr:HierarchicalSubject` (Lightroom hierarchical paths).

exiftool is invoked via `subprocess.run`; the binary must be on PATH.
Install via `apt install libimage-exiftool-perl` or `brew install exiftool`.

Idempotence: `write_sidecar` skips work when an existing sidecar's mtime
is newer than the image AND its `dc:Subject` set already matches what we
would write. See `specs/08-xmp-categories.md` for the design.
"""

import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

from .logging import get_logger

log = get_logger("phototag.xmp")

_EXIFTOOL_INSTALL_HINT = (
    "exiftool not found in PATH; install via `apt install libimage-exiftool-perl` or `brew install exiftool`"
)


def _require_exiftool() -> str:
    path = shutil.which("exiftool")
    if path is None:
        raise RuntimeError(_EXIFTOOL_INSTALL_HINT)
    return path


def sidecar_path(image_path: Path) -> Path:
    """Return the conventional `<image>.xmp` sidecar path."""
    # Convention: append ".xmp" to the full filename so `IMG_0001.jpg`
    # gets `IMG_0001.jpg.xmp`. This avoids collisions when two files share
    # a stem but differ in extension (`raw.cr2` + `raw.jpg`) — each gets
    # its own sidecar. digiKam / darktable read both forms; Lightroom
    # prefers the stem-only form, but the dual-extension form is the
    # safer default for mixed corpora.
    return image_path.with_suffix(image_path.suffix + ".xmp")


def _read_sidecar_subjects(exiftool: str, sidecar: Path) -> set[str]:
    """Return the current `dc:Subject` set from an existing sidecar."""
    try:
        proc = subprocess.run(
            [exiftool, "-j", "-XMP:Subject", "-XMP:HierarchicalSubject", str(sidecar)],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return set()
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return set()
    if not data:
        return set()
    subj = data[0].get("Subject")
    if subj is None:
        return set()
    if isinstance(subj, str):
        # exiftool collapses single-element lists to a scalar.
        return {subj}
    return {str(s) for s in subj}


def write_sidecar(
    image_path: Path,
    *,
    dc_subject: list[str],
    lr_hierarchical: list[str] | None = None,
) -> tuple[Path, bool]:
    """Write `<image>.xmp` with the given Dublin Core subjects.

    Returns `(sidecar_path, written)`. `written=False` means the existing
    sidecar already matched (mtime newer than the image AND subject set
    equals `dc_subject`) — caller can count it as "skipped".

    Atomic: writes to `<sidecar>.tmp.<token>` then renames into place so
    a crash never leaves a half-written XMP at the final path.

    Raises `RuntimeError` if `exiftool` is missing from PATH, or
    `subprocess.CalledProcessError` if exiftool itself fails.
    """
    exiftool = _require_exiftool()
    sidecar = sidecar_path(image_path)
    desired = sorted({s for s in dc_subject if s})
    desired_hier = sorted({s for s in (lr_hierarchical or []) if s})

    if sidecar.exists():
        try:
            sidecar_mtime = sidecar.stat().st_mtime
            image_mtime = image_path.stat().st_mtime
        except OSError:
            sidecar_mtime = 0.0
            image_mtime = 1.0  # force rewrite
        if sidecar_mtime >= image_mtime:
            current = _read_sidecar_subjects(exiftool, sidecar)
            if current == set(desired):
                log.debug("xmp_skip_unchanged", path=str(image_path))
                return sidecar, False

    # exiftool's `-o <sidecar> <image>` writes a fresh XMP-only sidecar
    # WITHOUT touching the source. `-overwrite_original` ensures we replace
    # any pre-existing file at the temp path without leaving a `_original`
    # backup. `-P` preserves the source mtime so subsequent scans see the
    # photo as unchanged.
    tmp = sidecar.with_name(f".{sidecar.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    if tmp.exists():
        tmp.unlink()
    cmd: list[str] = [exiftool, "-overwrite_original", "-P"]
    for s in desired:
        cmd.append(f"-XMP-dc:Subject={s}")
    for s in desired_hier:
        cmd.append(f"-XMP-lr:HierarchicalSubject={s}")
    cmd += ["-o", str(tmp), str(image_path)]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        os.replace(tmp, sidecar)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
    log.info(
        "xmp_written",
        path=str(image_path),
        sidecar=str(sidecar),
        subjects=len(desired),
        hierarchical=len(desired_hier),
    )
    return sidecar, True


def clean_sidecar(image_path: Path) -> bool:
    """Remove `<image>.xmp` if present. Returns True on actual deletion."""
    sidecar = sidecar_path(image_path)
    if not sidecar.exists():
        return False
    sidecar.unlink()
    log.info("xmp_removed", path=str(image_path), sidecar=str(sidecar))
    return True
