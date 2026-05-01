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


def test_category_cli_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`category add` / `map --tag` / `list` / `unmap --tag` / `rm` round-trip.

    Validates the CLI surface that #23 ships, end-to-end through Typer."""
    db = tmp_path / "phototag.db"
    s = Store(db)
    img = s.upsert_image(
        path=str(tmp_path / "x.jpg"),
        hash_="h",
        mtime=1.0,
        width=4,
        height=4,
        exif=None,
        processed_at=_now(),
    )
    s.replace_image_tags(img, "ram_plus", [("x-ray", 0.9)])
    s.close()

    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()

    # add → success
    r = runner.invoke(app, ["category", "add", "medical"])
    assert r.exit_code == 0, r.output

    # map tag → category
    r = runner.invoke(app, ["category", "map", "--tag", "x-ray", "--category", "medical"])
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    assert payload == {"tag": "x-ray", "category": "medical"}

    # list shows the rule
    r = runner.invoke(app, ["category", "list"])
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    assert payload["categories"] == [{"id": 1, "name": "medical"}]
    assert payload["tag_rules"] == [{"tag": "x-ray", "category": "medical"}]
    assert payload["cluster_rules"] == []

    # mapping to an unknown category fails cleanly with a non-zero exit code
    r = runner.invoke(app, ["category", "map", "--tag", "x-ray", "--category", "ghost"])
    assert r.exit_code == 1, r.output

    # passing both --tag and --cluster (or neither) returns usage error 2
    r = runner.invoke(app, ["category", "map", "--category", "medical"])
    assert r.exit_code == 2, r.output

    # unmap removes the rule
    r = runner.invoke(app, ["category", "unmap", "--tag", "x-ray"])
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    assert payload == {"tag": "x-ray", "removed": 1}

    # rm cascades cleanly
    r = runner.invoke(app, ["category", "rm", "medical"])
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    assert payload["deleted"] == 1


def test_category_per_image_override_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`phototag category map --image PATH --category C` round-trip (#27).

    Validates the CLI wiring of the new manual-override layer: map →
    `category list` shows it under `image_rules` → unmap drops it. Mutual
    exclusion with --tag/--cluster returns exit 2."""
    db = tmp_path / "phototag.db"
    img_path = tmp_path / "x.jpg"
    img_path.write_bytes(b"\xff\xd8\xff\xe0")  # minimal JPEG header — not decoded
    s = Store(db)
    img_id = s.upsert_image(
        path=str(img_path),
        hash_="h",
        mtime=1.0,
        width=4,
        height=4,
        exif=None,
        processed_at=_now(),
    )
    s.close()

    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()

    # Need a category first.
    r = runner.invoke(app, ["category", "add", "manual-pin"])
    assert r.exit_code == 0, r.output

    # Mutual-exclusion: --tag + --image → exit 2
    r = runner.invoke(
        app,
        ["category", "map", "--category", "manual-pin", "--tag", "x", "--image", str(img_path)],
    )
    assert r.exit_code == 2, r.output

    # Map by image
    r = runner.invoke(
        app,
        ["category", "map", "--category", "manual-pin", "--image", str(img_path)],
    )
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    assert payload["image_id"] == img_id
    assert payload["category"] == "manual-pin"

    # list shows the override under image_rules
    r = runner.invoke(app, ["category", "list"])
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    assert len(payload["image_rules"]) == 1
    assert payload["image_rules"][0]["category"] == "manual-pin"
    assert payload["image_rules"][0]["image_id"] == img_id

    # Unknown image path → exit 1
    r = runner.invoke(
        app,
        ["category", "map", "--category", "manual-pin", "--image", str(tmp_path / "missing.jpg")],
    )
    assert r.exit_code == 1, r.output

    # Unmap by image
    r = runner.invoke(app, ["category", "unmap", "--image", str(img_path)])
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    assert payload["image_id"] == img_id
    assert payload["removed"] == 1

    # Re-list shows empty image_rules
    r = runner.invoke(app, ["category", "list"])
    payload = _last_json(r.stdout)
    assert payload["image_rules"] == []


def _seed_query_db(tmp_path: Path) -> Path:
    """A DB with two images, tags, embeddings, and a named face on img1 —
    just enough to exercise `phototag query`'s filter paths."""
    db = tmp_path / "phototag.db"
    s = Store(db)
    img1 = s.upsert_image(
        path=str(tmp_path / "1.jpg"),
        hash_="h1",
        mtime=1.0,
        width=4,
        height=4,
        exif=None,
        processed_at=_now(),
    )
    img2 = s.upsert_image(
        path=str(tmp_path / "2.jpg"),
        hash_="h2",
        mtime=2.0,
        width=4,
        height=4,
        exif=None,
        processed_at=_now(),
    )
    s.replace_image_tags(img1, "ram_v1", [("cat", 0.9)])
    s.replace_image_tags(img2, "ram_v1", [("dog", 0.9)])
    # Two embeddings under the same model — query must pick one of them.
    s.upsert_embedding(img1, "fake-clip", np.array([1.0, 0.0], dtype=np.float32))
    s.upsert_embedding(img2, "fake-clip", np.array([0.0, 1.0], dtype=np.float32))
    # One named face on img1 so --person filtering has something to AND with.
    run = s.create_face_run({"manual": True}, _now())
    cid = s.add_face_cluster(run_id=run, cluster_no=0, size=1, label_auto=None, label_user="Anne")
    fid = s.insert_face(
        image_id=img1,
        bbox=[0, 0, 4, 4],
        det_score=0.9,
        embedding=np.ones(2, dtype=np.float32),
        model_name="m",
    )
    s.assign_face_to_cluster(fid, cid, 0.0)
    s.close()
    return db


def test_query_filter_short_circuits_without_clip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `--tag` filter that admits no images returns `[]` *without*
    booting the CLIP encoder (#28). Exercises the filter resolution path
    in isolation — no [clip] extra needed."""
    db = _seed_query_db(tmp_path)
    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["query", "anything", "--embedder", "fake-clip", "--tag", "ghost-tag"],
    )
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    assert payload == []


def test_query_person_filter_short_circuits_without_clip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown `--person` filter (no images carry that name) likewise
    short-circuits to `[]` before touching the CLIP encoder."""
    db = _seed_query_db(tmp_path)
    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["query", "anything", "--embedder", "fake-clip", "--person", "Ghost"],
    )
    assert r.exit_code == 0, r.output
    assert _last_json(r.stdout) == []


def test_query_with_clip_filter_intersects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When `--tag` admits exactly one image, `phototag query` returns
    only that image regardless of the embedding ranking. Skips if
    [clip] extra isn't installed (CI fast job)."""
    pytest.importorskip("open_clip")
    db = _seed_query_db(tmp_path)
    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["query", "anything", "--embedder", "fake-clip", "--tag", "cat", "--limit", "5"],
    )
    assert r.exit_code == 0, r.output
    payload = _last_json(r.stdout)
    # Only img1 carries the "cat" tag.
    assert len(payload) == 1
    assert payload[0]["path"].endswith("1.jpg")
