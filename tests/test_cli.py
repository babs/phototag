import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from typer.testing import CliRunner

from phototag import __version__
from phototag.cli import app
from phototag.store import Store


def test_version_command() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _seed_two_images(tmp_path: Path) -> Path:
    db = tmp_path / "phototag.db"
    real = tmp_path / "real.jpg"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(real, format="JPEG")
    s = Store(db)
    a = s.upsert_image(
        path=str(real), hash_="r", mtime=1.0, width=4, height=4, exif=None, processed_at=_now()
    )
    b = s.upsert_image(
        path=str(tmp_path / "b.jpg"),
        hash_="b",
        mtime=2.0,
        width=4,
        height=4,
        exif=None,
        processed_at=_now(),
    )
    s.replace_image_tags(a, "ram_v1", [("cat", 0.9), ("smile", 0.8)])
    s.replace_image_tags(b, "ram_v1", [("dog", 0.95)])
    s.replace_image_tags(b, "geo_v1", [("paris", 1.0)])
    s.close()
    return db


def _last_json(stdout: str) -> Any:
    # structlog logs ONE-line JSON to stdout above our payload; the typer
    # echo writes pretty-printed (indented) JSON. Skip lines that look like
    # log records (start with '{"' on a single line) and parse the rest.
    lines = stdout.splitlines(keepends=True)
    keep: list[str] = []
    for ln in lines:
        s = ln.lstrip()
        if not keep and s.startswith('{"') and s.rstrip().endswith("}") and "event" in s:
            continue
        keep.append(ln)
    return json.loads("".join(keep).strip())


def test_list_filters_by_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _seed_two_images(tmp_path)
    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    r = runner.invoke(app, ["list", "--tag", "cat"])
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    assert {row["path"] for row in payload} == {str(tmp_path / "real.jpg")}


def test_stats_excludes_geo_when_kind_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _seed_two_images(tmp_path)
    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    r = runner.invoke(app, ["stats", "--kind", "label"])
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    names = {t["name"] for t in payload["top_tags"]}
    assert "paris" not in names  # geo
    assert {"cat", "smile", "dog"} <= names


def test_export_json_then_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _seed_two_images(tmp_path)
    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    out_json = tmp_path / "out.json"
    r = runner.invoke(app, ["export", "--out", str(out_json)])
    assert r.exit_code == 0, r.output
    data = json.loads(out_json.read_text())
    assert len(data) == 2
    out_csv = tmp_path / "out.csv"
    r = runner.invoke(app, ["export", "--format", "csv", "--out", str(out_csv)])
    assert r.exit_code == 0, r.output
    assert "cat:0.90" in out_csv.read_text()


def test_prune_dry_run_then_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`prune` flags missing-on-disk rows in dry-run; --apply deletes them."""
    db = tmp_path / "phototag.db"
    real = tmp_path / "real.jpg"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(real, format="JPEG")

    s = Store(db)
    real_id = s.upsert_image(
        path=str(real), hash_="r", mtime=1.0, width=4, height=4, exif=None, processed_at=_now()
    )
    missing_id = s.upsert_image(
        path=str(tmp_path / "gone.jpg"),
        hash_="g",
        mtime=1.0,
        width=4,
        height=4,
        exif=None,
        processed_at=_now(),
    )
    s.close()

    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    # dry-run: reports 1 missing, deletes 0
    result = runner.invoke(app, ["prune"])
    assert result.exit_code == 0, result.output
    payload = _last_json(result.stdout)
    assert payload["missing"] == 1
    assert payload["deleted"] == 0
    s = Store(db)
    assert s.get_image(missing_id) is not None
    s.close()
    # --apply: deletes the missing row
    result = runner.invoke(app, ["prune", "--apply"])
    assert result.exit_code == 0, result.output
    payload = _last_json(result.stdout)
    assert payload["deleted"] == 1
    s = Store(db)
    assert s.get_image(missing_id) is None
    assert s.get_image(real_id) is not None
    s.close()
