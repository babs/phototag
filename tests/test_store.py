from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from phototag.store import Store


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def test_migrate_creates_schema(tmp_db: Path) -> None:
    store = Store(tmp_db)
    try:
        version = store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert int(version["value"]) >= 3
    finally:
        store.close()


def test_upsert_and_lookup_image(tmp_db: Path) -> None:
    store = Store(tmp_db)
    try:
        image_id = store.upsert_image(
            path="/tmp/a.jpg",
            hash_="deadbeef",
            mtime=1.0,
            width=10,
            height=20,
            exif=None,
            processed_at=_now(),
        )
        row = store.get_image_by_path("/tmp/a.jpg")
        assert row is not None
        assert row.id == image_id
        assert row.hash == "deadbeef"
        assert row.width == 10
    finally:
        store.close()


def test_replace_image_tags(tmp_db: Path) -> None:
    store = Store(tmp_db)
    try:
        image_id = store.upsert_image(
            path="/tmp/a.jpg",
            hash_="d",
            mtime=1.0,
            width=10,
            height=10,
            exif=None,
            processed_at=_now(),
        )
        store.replace_image_tags(image_id, "ram_v1", [("cat", 0.9), ("animal", 0.8)])
        tags = store.list_tags_for_image(image_id)
        assert len(tags) == 2
        assert tags[0][0] == "cat"
        assert tags[0][1] == 0.9
        # replacing should drop old tags from the same model
        store.replace_image_tags(image_id, "ram_v1", [("dog", 0.7)])
        tags = store.list_tags_for_image(image_id)
        assert len(tags) == 1
        assert tags[0][0] == "dog"
    finally:
        store.close()


def test_embedding_roundtrip(tmp_db: Path) -> None:
    store = Store(tmp_db)
    try:
        image_id = store.upsert_image(
            path="/tmp/a.jpg",
            hash_="d",
            mtime=1.0,
            width=10,
            height=10,
            exif=None,
            processed_at=_now(),
        )
        v = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        store.upsert_embedding(image_id, "clip_v1", v)
        assert store.has_embedding(image_id, "clip_v1")
        ids, mat = store.load_embeddings("clip_v1")
        assert ids == [image_id]
        np.testing.assert_array_equal(mat[0], v)
    finally:
        store.close()
