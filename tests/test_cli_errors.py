"""Coverage for CLI **error branches** — exit codes, hostile input,
empty-state guards. The happy-path coverage lives in `test_cli.py`;
this file targets the operational guardrails users actually hit when
something is wrong (typo'd format, no embeddings yet, missing file,
etc.) so the next "phototag faces auto-attach errored, what now?"
debugging session has a regression net.

Kept deliberately ML-extra-free: no real RAM/CLIP/InsightFace imports.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from phototag.cli import app
from phototag.store import Store


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _seed_image(tmp_path: Path, name: str = "real.jpg") -> tuple[Path, Path, int]:
    """Write one DB with a single image row pointing at a real file on disk.
    Returns (db_path, image_path, image_id) so callers can mutate either side.
    """
    db = tmp_path / "phototag.db"
    img = tmp_path / name
    Image.new("RGB", (4, 4), (1, 2, 3)).save(img, format="JPEG")
    s = Store(db)
    image_id = s.upsert_image(
        path=str(img), hash_="h", mtime=1.0, width=4, height=4, exif=None, processed_at=_now()
    )
    s.close()
    return db, img, image_id


def test_export_rejects_unknown_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--format xml` (or anything not json/csv) must exit non-zero with a
    clean BadParameter, not silently default to one of them."""
    db, _img, _ = _seed_image(tmp_path)
    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    r = runner.invoke(app, ["export", "--format", "xml"])
    # typer.BadParameter exits with code 2 (UsageError convention).
    assert r.exit_code != 0
    assert "json" in (r.output + (r.stderr or ""))


def test_query_errors_when_no_embeddings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`phototag query` short-circuits BEFORE booting the CLIP encoder
    when the DB has zero embedding rows — surfaces a `Run phototag embed
    first` hint via BadParameter. The test asserts we never even get to
    the ClipEmbedder import (would otherwise need the [clip] extra)."""
    db, _img, _ = _seed_image(tmp_path)
    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    r = runner.invoke(app, ["query", "anything"])
    assert r.exit_code != 0
    combined = r.output + (r.stderr or "")
    assert "phototag embed" in combined or "No embeddings" in combined


def test_info_exits_one_when_image_not_in_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`phototag info <missing>` must exit 1 (script-friendly), not raise.
    Users chain this with shell `&&`; raising would print a traceback."""
    db, _img, _ = _seed_image(tmp_path)
    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    r = runner.invoke(app, ["info", str(tmp_path / "ghost.jpg")])
    assert r.exit_code == 1, r.output
    assert "not in DB" in (r.output + (r.stderr or ""))


def test_info_returns_image_payload_when_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy-path companion to the missing-image test — also covers the
    JSON-emit branch (line 646-653) that the missing path skips."""
    import json

    db, img, _ = _seed_image(tmp_path)
    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    r = runner.invoke(app, ["info", str(img)])
    assert r.exit_code == 0, r.output
    # Skip structlog log lines if any precede the payload.
    payload_text = r.stdout[r.stdout.index("{\n") :]
    payload = json.loads(payload_text[: payload_text.rindex("}") + 1])
    assert payload["path"] == str(img)
    assert payload["size"] == [4, 4]
    assert payload["tags"] == []


def test_rename_exits_one_for_unknown_cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`phototag rename <bogus_id> <label>` must exit 1 — the parallel
    rename-bulk path skips with a stderr message; the single-id path
    bails entirely. Both behaviors are intentional and tested separately."""
    db, _img, _ = _seed_image(tmp_path)
    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    r = runner.invoke(app, ["rename", "99999", "Anne"])
    assert r.exit_code == 1, r.output
    assert "not found" in (r.output + (r.stderr or ""))


def test_rename_clears_label_with_empty_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty-string label clears the `label_user` field — documented in
    the command help. Regression guard for the `label or None` collapse
    at line 674: if anyone "fixes" it to truthy-only, this breaks."""
    db, _img, _ = _seed_image(tmp_path)
    s = Store(db)
    try:
        with s.transaction():
            run = s.create_cluster_run({"src": "test"}, _now())
            cid = s.add_cluster(run_id=run, cluster_no=0, size=1, label_auto="A")
            s.set_cluster_label_user(cid, "Original")
    finally:
        s.close()
    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    r = runner.invoke(app, ["rename", str(cid), ""])
    assert r.exit_code == 0, r.output
    s = Store(db)
    try:
        c = s.get_cluster(cid)
        assert c is not None
        assert c["label_user"] is None
    finally:
        s.close()


def test_prune_dry_run_lists_missing_without_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`prune` (no --apply) reports `missing` and emits a hint pointing
    at --apply, but the row stays in the DB. Defensive default: never
    delete on a typo'd corpus path."""
    import json as _json

    db, img, image_id = _seed_image(tmp_path)
    img.unlink()  # make the row a ghost
    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    r = runner.invoke(app, ["prune"])
    assert r.exit_code == 0, r.output
    payload_text = r.stdout[r.stdout.index("{\n") :]
    payload = _json.loads(payload_text[: payload_text.rindex("}") + 1])
    assert payload["checked"] == 1
    assert payload["missing"] == 1
    assert payload["deleted"] == 0
    assert "--apply" in payload.get("hint", "")
    # Row still in DB.
    s = Store(db)
    try:
        assert s.get_image(image_id) is not None
    finally:
        s.close()


def test_prune_apply_deletes_missing_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`prune --apply` actually drops the rows. Cluster/tag cascades come
    from FK ON DELETE — not exercised here; the row count is the contract."""
    import json as _json

    db, img, image_id = _seed_image(tmp_path)
    img.unlink()
    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    r = runner.invoke(app, ["prune", "--apply"])
    assert r.exit_code == 0, r.output
    payload_text = r.stdout[r.stdout.index("{\n") :]
    payload = _json.loads(payload_text[: payload_text.rindex("}") + 1])
    assert payload["missing"] == 1
    assert payload["deleted"] == 1
    s = Store(db)
    try:
        assert s.get_image(image_id) is None
    finally:
        s.close()


def test_exif_backfill_skips_when_already_populated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`exif-backfill` without --force skips rows whose `exif_json` is
    already populated. Idempotency guard: re-running on a synced library
    must not re-stat every file."""
    import json as _json

    db = tmp_path / "phototag.db"
    img = tmp_path / "scene.jpg"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(img, format="JPEG")
    s = Store(db)
    image_id = s.upsert_image(
        path=str(img),
        hash_="h",
        mtime=1.0,
        width=4,
        height=4,
        exif={"camera": "Synthetic"},
        processed_at=_now(),
    )
    assert image_id  # silence unused-warning; we only care about the row.
    s.close()
    monkeypatch.setenv("APP_DB_PATH", str(db))
    runner = CliRunner()
    r = runner.invoke(app, ["exif-backfill"])
    assert r.exit_code == 0, r.output
    payload_text = r.stdout[r.stdout.index("{\n") :]
    payload = _json.loads(payload_text[: payload_text.rindex("}") + 1])
    assert payload["total"] == 1
    assert payload["skipped"] == 1
    assert payload["updated"] == 0
