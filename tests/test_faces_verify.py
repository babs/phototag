"""Tests for `phototag.faces.verify_faces` (heuristic pipeline)."""

from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from phototag.faces import verify_faces
from phototag.store import Store


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _seed(store: Store) -> tuple[int, int, int]:
    """Insert one image and three faces: one good, one low-score, one tiny."""
    img = store.upsert_image(
        path="/tmp/a.jpg",
        hash_="h",
        mtime=1.0,
        width=200,
        height=200,
        exif=None,
        processed_at=_now(),
    )
    good = store.insert_face(
        image_id=img,
        bbox=[10, 10, 80, 80],  # 80×80 = 6400 px²
        det_score=0.95,
        embedding=np.ones(2, dtype=np.float32),
        model_name="m",
    )
    bad_score = store.insert_face(
        image_id=img,
        bbox=[20, 20, 80, 80],
        det_score=0.40,  # below default 0.65
        embedding=np.ones(2, dtype=np.float32),
        model_name="m",
    )
    tiny = store.insert_face(
        image_id=img,
        bbox=[5, 5, 20, 20],  # 20×20 = 400 px², below default 1024
        det_score=0.95,
        embedding=np.ones(2, dtype=np.float32),
        model_name="m",
    )
    return good, bad_score, tiny


def test_verify_dry_run_flags_only(tmp_db: Path) -> None:
    store = Store(tmp_db)
    try:
        good, bad_score, tiny = _seed(store)
        counts = verify_faces(store, apply=False)
        assert counts["checked"] == 3
        assert counts["kept"] == 1
        assert counts["low_score"] == 1
        assert counts["small"] == 1
        assert counts["deleted"] == 0
        # All rows still there.
        assert store.count_faces() == 3
        # Verified column set.
        rows = {int(r["id"]): r["verified"] for r in store.conn.execute("SELECT id, verified FROM faces")}
        assert rows[good] == 1
        assert rows[bad_score] == 0
        assert rows[tiny] == 0
    finally:
        store.close()


def test_verify_apply_deletes(tmp_db: Path) -> None:
    store = Store(tmp_db)
    try:
        good, _, _ = _seed(store)
        counts = verify_faces(store, apply=True)
        assert counts["deleted"] == 2
        # Only the good face left.
        assert store.count_faces() == 1
        remaining = next(store.conn.execute("SELECT id FROM faces"))
        assert remaining["id"] == good
    finally:
        store.close()


def test_verify_thresholds(tmp_db: Path) -> None:
    store = Store(tmp_db)
    try:
        _seed(store)
        # Lower threshold: 0.4 face passes
        counts = verify_faces(store, min_det_score=0.3, min_area=100, apply=False)
        assert counts["kept"] == 3
        assert counts["low_score"] == 0
        assert counts["small"] == 0
    finally:
        store.close()
