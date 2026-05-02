"""Unit tests for `phototag.xmp` paths that do NOT subprocess to exiftool.

The main `test_xmp.py` is gated on `shutil.which("exiftool")` and skips
wholesale when the binary isn't present (CI minimal image, sandboxed
dev box). These tests cover the non-exiftool code paths so the module
gets meaningful coverage even when exiftool is unavailable:

  - `sidecar_path()` — pure path arithmetic
  - `_require_exiftool()` — RuntimeError when binary missing
  - `clean_sidecar()` — no-op when sidecar doesn't exist; deletes when it does
  - `write_sidecar()` — fails fast with RuntimeError when exiftool missing
"""

from pathlib import Path

import pytest

from phototag import xmp
from phototag.xmp import clean_sidecar, sidecar_path, write_sidecar


def test_sidecar_path_appends_xmp_to_full_filename(tmp_path: Path) -> None:
    """Convention: `IMG_0001.jpg` → `IMG_0001.jpg.xmp` so dual-extension
    siblings (`raw.cr2` + `raw.jpg`) get distinct sidecars. Regression
    guard if anyone "fixes" this to the stem-only form (Lightroom prefers
    that, but breaks mixed-format corpora)."""
    img = tmp_path / "IMG_0001.jpg"
    assert sidecar_path(img) == tmp_path / "IMG_0001.jpg.xmp"
    raw = tmp_path / "shoot.cr2"
    assert sidecar_path(raw) == tmp_path / "shoot.cr2.xmp"


def test_require_exiftool_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_require_exiftool()` must surface a clear install hint, not a
    generic `FileNotFoundError` from subprocess. The hint is what users
    see when they install the `[xmp]` extra without the system binary."""
    monkeypatch.setattr(xmp.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="exiftool not found"):
        xmp._require_exiftool()


def test_write_sidecar_raises_when_exiftool_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """write_sidecar must fail fast (before touching the filesystem) when
    exiftool isn't installed. Matters operationally: a CLI bulk run
    against a fresh box should error on the first image, not write
    half-broken sidecars from a partial state."""
    img = tmp_path / "x.jpg"
    img.write_bytes(b"\xff\xd8\xff\xd9")  # minimal JPEG bytes; never decoded
    monkeypatch.setattr(xmp.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="exiftool not found"):
        write_sidecar(img, dc_subject=["any"])
    # No sidecar / no temp file leaked.
    assert not sidecar_path(img).exists()
    assert list(tmp_path.glob(".*tmp")) == []


def test_clean_sidecar_no_op_when_absent(tmp_path: Path) -> None:
    """`clean_sidecar` returns False (not raise) when no sidecar exists.
    Bulk `phototag xmp clean --apply` relies on this to be idempotent."""
    img = tmp_path / "missing.jpg"
    assert clean_sidecar(img) is False


def test_clean_sidecar_deletes_existing_file(tmp_path: Path) -> None:
    """Direct deletion path — exercises clean_sidecar without needing
    write_sidecar (which subprocesses to exiftool). Hand-craft a fake
    sidecar file at the conventional path."""
    img = tmp_path / "y.jpg"
    sc = sidecar_path(img)
    sc.write_text("<x:xmpmeta/>")
    assert sc.exists()
    assert clean_sidecar(img) is True
    assert not sc.exists()
    # Second call no-ops, same as the no-sidecar case.
    assert clean_sidecar(img) is False
