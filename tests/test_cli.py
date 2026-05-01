import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
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


def test_doctor_detects_and_fixes_size_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`phototag doctor` flags face_clusters.size mismatch; --fix recomputes."""
    db = tmp_path / "phototag.db"
    s = Store(db)
    img = s.upsert_image(
        path=str(tmp_path / "a.jpg"),
        hash_="h",
        mtime=1.0,
        width=4,
        height=4,
        exif=None,
        processed_at=_now(),
    )
    run = s.create_face_run({}, _now())
    cid = s.add_face_cluster(run_id=run, cluster_no=0, size=99, label_auto=None)  # wrong size
    f = s.insert_face(
        image_id=img,
        bbox=[0, 0, 10, 10],
        det_score=0.9,
        embedding=np.ones(2, dtype=np.float32),
        model_name="m",
    )
    s.assign_face_to_cluster(f, cid, 0.0)  # actual count = 1, recorded = 99
    s.close()

    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    assert payload["ok"] is False
    assert "face_cluster_size_mismatch" in payload["issues"]

    r = runner.invoke(app, ["doctor", "--fix"])
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    assert payload["fix_applied"] is True
    assert payload["issues"].get("face_cluster_size_fixed") == 1
    # `ok` must reflect the post-fix state. The original
    # face_cluster_size_mismatch key is replaced by `_fixed`, so nothing
    # un-resolved remains and `ok` should flip true. Scripts chain
    # `phototag doctor --fix && next-step` on this signal.
    assert payload["ok"] is True
    assert "face_cluster_size_mismatch" not in payload["issues"]
    s = Store(db)
    row = s.conn.execute("SELECT size FROM face_clusters WHERE id=?", (cid,)).fetchone()
    assert row["size"] == 1
    s.close()


def test_faces_corrections_compact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`faces corrections-compact` keeps only the most-recent row per face_id."""
    db = _seed_two_images(tmp_path)
    s = Store(db)
    # 5 corrections for face_id=1, 3 for face_id=2 — total 8 rows, 2 survivors.
    for i in range(5):
        s.log_face_correction(face_id=1, image_id=1, action="named", name=f"n{i}")
    for i in range(3):
        s.log_face_correction(face_id=2, image_id=1, action="named", name=f"m{i}")
    s.conn.commit()
    s.close()

    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    # dry-run: reports 6 to delete, no actual change
    r = runner.invoke(app, ["faces", "corrections-compact"])
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    assert payload["before"] == 8
    assert payload["survivors"] == 2
    assert payload["projected_delete"] == 6
    assert payload["deleted"] == 0
    s = Store(db)
    assert len(s.list_face_corrections()) == 8
    s.close()
    # --apply: collapses to one row per face_id
    r = runner.invoke(app, ["faces", "corrections-compact", "--apply"])
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    assert payload["deleted"] == 6
    s = Store(db)
    rows = s.list_face_corrections()
    assert len(rows) == 2
    by_face = {r["face_id"]: r for r in rows}
    # The most-recent name (highest id) per face survives.
    assert by_face[1]["name"] == "n4"
    assert by_face[2]["name"] == "m2"
    s.close()


def test_backup_creates_atomic_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`phototag backup` writes an atomic SQLite snapshot containing seeded rows."""
    db = _seed_two_images(tmp_path)
    monkeypatch.setenv("APP_DB_PATH", str(db))
    snap = tmp_path / "snap.db"

    runner = CliRunner()
    result = runner.invoke(app, ["backup", "--out", str(snap)])
    assert result.exit_code == 0, result.output
    payload = _last_json(result.stdout)
    assert payload["dst"] == str(snap)
    assert payload["src"] == str(db)
    assert payload["bytes"] > 0
    assert "took_ms" in payload
    assert snap.exists()
    assert snap.stat().st_size == payload["bytes"]
    # No half-file left behind from the atomic-rename dance.
    assert not snap.with_suffix(snap.suffix + ".tmp").exists()

    # The snapshot is a working DB containing the seeded rows.
    s = Store(snap)
    rows = list(s.iter_images())
    assert {r.path for r in rows} == {str(tmp_path / "real.jpg"), str(tmp_path / "b.jpg")}
    s.close()


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
