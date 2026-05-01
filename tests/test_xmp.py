"""Tests for `phototag.xmp` (the exiftool-backed sidecar writer).

The whole module subprocesses out to `exiftool`. When the binary is not
installed (CI minimal image, sandboxed dev box) the tests skip
gracefully so the rest of the suite still runs.
"""

import json
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from typer.testing import CliRunner

from phototag.cli import app
from phototag.store import Store
from phototag.xmp import clean_sidecar, sidecar_path, write_sidecar

pytestmark = pytest.mark.skipif(
    shutil.which("exiftool") is None,
    reason="exiftool not installed (apt install libimage-exiftool-perl / brew install exiftool)",
)


def _make_jpeg(path: Path) -> None:
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path, format="JPEG")


def _read_subjects(sidecar: Path) -> list[str]:
    proc = subprocess.run(
        ["exiftool", "-j", "-XMP:Subject", str(sidecar)],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout or "[]")
    if not data:
        return []
    subj = data[0].get("Subject")
    if subj is None:
        return []
    if isinstance(subj, str):
        return [subj]
    return [str(s) for s in subj]


def test_write_sidecar_round_trip(tmp_path: Path) -> None:
    img = tmp_path / "cat.jpg"
    _make_jpeg(img)
    sidecar, written = write_sidecar(img, dc_subject=["cat", "kitten"])
    assert written is True
    assert sidecar == sidecar_path(img)
    assert sidecar.exists()
    # The original photo is never touched — exiftool wrote the XMP only.
    assert img.exists()
    subjects = _read_subjects(sidecar)
    assert sorted(subjects) == ["cat", "kitten"]


def test_write_sidecar_idempotent(tmp_path: Path) -> None:
    img = tmp_path / "dog.jpg"
    _make_jpeg(img)
    _sidecar, first = write_sidecar(img, dc_subject=["dog", "puppy"])
    assert first is True
    # Same input again — must skip the exiftool re-write.
    _sidecar, second = write_sidecar(img, dc_subject=["dog", "puppy"])
    assert second is False


def test_write_sidecar_rewrites_when_subjects_change(tmp_path: Path) -> None:
    img = tmp_path / "x.jpg"
    _make_jpeg(img)
    write_sidecar(img, dc_subject=["a"])
    # Bump mtime so the freshness gate fires regardless of fs resolution.
    time.sleep(0.01)
    _sidecar, written = write_sidecar(img, dc_subject=["a", "b"])
    assert written is True
    assert sorted(_read_subjects(sidecar_path(img))) == ["a", "b"]


def test_clean_sidecar_removes_file(tmp_path: Path) -> None:
    img = tmp_path / "y.jpg"
    _make_jpeg(img)
    write_sidecar(img, dc_subject=["bird"])
    side = sidecar_path(img)
    assert side.exists()
    assert clean_sidecar(img) is True
    assert not side.exists()
    # Second call no-ops.
    assert clean_sidecar(img) is False


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _last_json(stdout: str) -> dict[str, Any]:
    """Skip structlog one-line JSON above the typer-echoed payload."""
    lines = stdout.splitlines(keepends=True)
    keep: list[str] = []
    for ln in lines:
        s = ln.lstrip()
        if not keep and s.startswith('{"') and s.rstrip().endswith("}") and "event" in s:
            continue
        keep.append(ln)
    parsed: dict[str, Any] = json.loads("".join(keep).strip())
    return parsed


def test_xmp_write_cli_dry_run_then_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "phototag.db"
    img = tmp_path / "scene.jpg"
    _make_jpeg(img)
    s = Store(db)
    img_id = s.upsert_image(
        path=str(img), hash_="h", mtime=1.0, width=8, height=8, exif=None, processed_at=_now()
    )
    s.replace_image_tags(img_id, "ram_v1", [("cat", 0.9), ("low", 0.2)])
    s.close()

    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()

    # Dry-run reports a plan, writes nothing.
    r = runner.invoke(app, ["xmp", "write", str(tmp_path), "--threshold", "0.5"])
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    assert payload["dry_run"] is True
    assert payload["considered"] == 1
    assert payload["plans"][0]["subjects"] == ["cat"]  # "low" filtered by threshold
    assert not sidecar_path(img).exists()

    # Apply actually writes.
    r = runner.invoke(app, ["xmp", "write", str(tmp_path), "--threshold", "0.5", "--apply"])
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    assert payload["written"] == 1
    assert payload["failed"] == 0
    assert sidecar_path(img).exists()
    assert _read_subjects(sidecar_path(img)) == ["cat"]


def test_xmp_clean_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "phototag.db"
    img = tmp_path / "scene.jpg"
    _make_jpeg(img)
    s = Store(db)
    s.upsert_image(path=str(img), hash_="h", mtime=1.0, width=8, height=8, exif=None, processed_at=_now())
    s.close()
    write_sidecar(img, dc_subject=["x"])
    assert sidecar_path(img).exists()

    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    # Dry-run leaves the sidecar in place.
    r = runner.invoke(app, ["xmp", "clean", str(tmp_path)])
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    assert payload["dry_run"] is True
    assert payload["considered"] == 1
    assert payload["removed"] == 0
    assert sidecar_path(img).exists()
    # Apply removes it.
    r = runner.invoke(app, ["xmp", "clean", str(tmp_path), "--apply"])
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    assert payload["removed"] == 1
    assert not sidecar_path(img).exists()
