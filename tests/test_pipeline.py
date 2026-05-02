"""End-to-end coverage for `phototag.pipeline.{scan_and_tag,embed_all}` (#29).

Uses `tests/fixtures/fakes.py` so we exercise the real orchestration code
(decode + hash + producer/consumer + transactional persist) without
needing the [ram] / [clip] extras. Validates:
  - scan idempotence (second run on same folder = no work)
  - --force re-tags everything
  - --force-tag re-runs inference but skips re-hashing
  - decode failure → counted, doesn't poison the batch
  - embed_all skip-existing + --force semantics
  - scan + embed full round-trip with a 5-image corpus
"""

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from phototag.pipeline import embed_all, scan_and_tag
from phototag.store import Store
from tests.fixtures.fakes import FakeEmbedder, FakeTagger


def _seed_corpus(root: Path, n: int = 5) -> list[Path]:
    """Write `n` distinct JPEGs under `root`. Each gets unique pixel
    content so the FakeTagger / FakeEmbedder produce unique outputs."""
    root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i in range(n):
        p = root / f"img_{i:03d}.jpg"
        # Per-image color tuples → distinct pixel bytes → distinct hashes.
        Image.new("RGB", (8 + i, 8 + i), (10 * i, 20 * i, 30 * i)).save(p, format="JPEG")
        paths.append(p)
    return paths


def test_scan_and_tag_basic_round_trip(tmp_path: Path, tmp_db: Path) -> None:
    _seed_corpus(tmp_path / "corpus", n=5)
    store = Store(tmp_db)
    try:
        tagger = FakeTagger(threshold=0.0)  # accept everything for visibility
        counts = scan_and_tag(tmp_path / "corpus", store, tagger, batch_size=2)
        assert counts == {"scanned": 5, "skipped": 0, "tagged": 5, "failed": 0}
        # Tags actually persisted.
        assert all(store.list_image_tags(row.id, min_score=0.0) for row in store.iter_images())
    finally:
        store.close()


def test_scan_and_tag_idempotent(tmp_path: Path, tmp_db: Path) -> None:
    """Second `scan_and_tag` on the same corpus must skip every file
    (gated on `(path, hash, mtime)`)."""
    _seed_corpus(tmp_path / "corpus", n=3)
    store = Store(tmp_db)
    try:
        tagger = FakeTagger(threshold=0.0)
        first = scan_and_tag(tmp_path / "corpus", store, tagger)
        assert first["tagged"] == 3
        second = scan_and_tag(tmp_path / "corpus", store, tagger)
        # Cheap-pass skips before decode → 'skipped' goes up, 'tagged'
        # stays at 0.
        assert second["scanned"] == 3
        assert second["skipped"] == 3
        assert second["tagged"] == 0
        assert second["failed"] == 0
    finally:
        store.close()


def test_scan_and_tag_force_retags(tmp_path: Path, tmp_db: Path) -> None:
    """`--force` (force=True) makes every row re-decode + re-hash + re-tag
    even when nothing changed on disk."""
    _seed_corpus(tmp_path / "corpus", n=3)
    store = Store(tmp_db)
    try:
        tagger = FakeTagger(threshold=0.0)
        scan_and_tag(tmp_path / "corpus", store, tagger)
        forced = scan_and_tag(tmp_path / "corpus", store, tagger, force=True)
        assert forced["tagged"] == 3
        # `skipped` stays 0 because force blows past the cheap-pass gate
        # AND past the per-batch (hash, mtime) check.
        assert forced["skipped"] == 0
    finally:
        store.close()


def test_scan_and_tag_force_tag_only(tmp_path: Path, tmp_db: Path) -> None:
    """`--force-tag` re-runs inference but DOES NOT re-hash (the cheap-
    pass mtime gate is also bypassed). Use case: bumping RAM++ version
    without rehashing the corpus."""
    _seed_corpus(tmp_path / "corpus", n=3)
    store = Store(tmp_db)
    try:
        tagger = FakeTagger(threshold=0.0)
        scan_and_tag(tmp_path / "corpus", store, tagger)
        ft = scan_and_tag(tmp_path / "corpus", store, tagger, force_tag=True)
        assert ft["tagged"] == 3
    finally:
        store.close()


def test_scan_and_tag_decode_failure_counted(tmp_path: Path, tmp_db: Path) -> None:
    """A truncated / corrupt file is counted as `failed`, not `tagged`,
    and doesn't crash the batch — the rest of the corpus still tags."""
    corpus = tmp_path / "corpus"
    _seed_corpus(corpus, n=3)
    # Drop a truncated "JPEG" alongside the real ones.
    bad = corpus / "broken.jpg"
    bad.write_bytes(b"\xff\xd8\xff\xe0not a real jpeg")
    store = Store(tmp_db)
    try:
        tagger = FakeTagger(threshold=0.0)
        counts = scan_and_tag(corpus, store, tagger, batch_size=2)
        assert counts["scanned"] == 4
        assert counts["tagged"] == 3
        assert counts["failed"] == 1
    finally:
        store.close()


def test_scan_and_tag_threshold_filters(tmp_path: Path, tmp_db: Path) -> None:
    """Tags below the FakeTagger threshold are dropped before persist."""
    _seed_corpus(tmp_path / "corpus", n=2)
    store = Store(tmp_db)
    try:
        # Threshold 0.99 → almost every random tag drops out (rng range
        # is [0.5, 1.0), so very few survive on average).
        tagger = FakeTagger(threshold=0.99)
        scan_and_tag(tmp_path / "corpus", store, tagger)
        kept = sum(len(store.list_image_tags(r.id)) for r in store.iter_images())
        # Most tags dropped; can't assert exact count (rng-dependent) but
        # it's strictly less than the unfiltered baseline of 6 (2 imgs × 3).
        assert kept < 6
    finally:
        store.close()


def test_embed_all_skip_existing(tmp_path: Path, tmp_db: Path) -> None:
    """`embed_all` second run: every row already has an embedding for the
    embedder's `name`, so `skipped` covers the corpus."""
    _seed_corpus(tmp_path / "corpus", n=3)
    store = Store(tmp_db)
    try:
        scan_and_tag(tmp_path / "corpus", store, FakeTagger(threshold=0.0))
        emb = FakeEmbedder(dim=8)
        first = embed_all(store, emb)
        assert first["embedded"] == 3
        assert first["skipped"] == 0
        second = embed_all(store, emb)
        assert second["embedded"] == 0
        assert second["skipped"] == 3
    finally:
        store.close()


def test_embed_all_force(tmp_path: Path, tmp_db: Path) -> None:
    """`force=True` re-embeds every row regardless of prior state."""
    _seed_corpus(tmp_path / "corpus", n=3)
    store = Store(tmp_db)
    try:
        scan_and_tag(tmp_path / "corpus", store, FakeTagger(threshold=0.0))
        emb = FakeEmbedder(dim=8)
        embed_all(store, emb)
        forced = embed_all(store, emb, force=True)
        assert forced["embedded"] == 3
        assert forced["skipped"] == 0
    finally:
        store.close()


def test_embed_all_persists_unit_norm(tmp_path: Path, tmp_db: Path) -> None:
    """Embeddings round-trip through SQLite preserving the unit-norm
    contract that semantic search relies on."""
    _seed_corpus(tmp_path / "corpus", n=4)
    store = Store(tmp_db)
    try:
        scan_and_tag(tmp_path / "corpus", store, FakeTagger(threshold=0.0))
        emb = FakeEmbedder(dim=12)
        embed_all(store, emb)
        ids, mat = store.load_embeddings(emb.name)
        assert mat.shape == (4, 12)
        norms = np.linalg.norm(mat, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-5)
        assert len(ids) == 4
    finally:
        store.close()


def test_scan_then_embed_full_pipeline(tmp_path: Path, tmp_db: Path) -> None:
    """End-to-end: scan + embed produces a populated DB ready for
    clustering / search. Uses tag_overrides so a known tag survives the
    threshold filter for assertion."""
    _seed_corpus(tmp_path / "corpus", n=2)
    store = Store(tmp_db)
    try:
        tagger = FakeTagger(
            threshold=0.5,
            tag_overrides={(8, 8): [("cat", 0.9), ("kitten", 0.8)]},
        )
        scan_counts = scan_and_tag(tmp_path / "corpus", store, tagger)
        assert scan_counts["tagged"] == 2
        embed_counts = embed_all(store, FakeEmbedder(dim=16))
        assert embed_counts["embedded"] == 2

        # The (8, 8) photo carries the override tags.
        for row in store.iter_images():
            if row.width == 8 and row.height == 8:
                tags = {n for n, _ in store.list_image_tags(row.id)}
                assert "cat" in tags
                assert "kitten" in tags
                break
        else:
            pytest.fail("did not find the 8x8 image in the DB")
    finally:
        store.close()


def test_decode_one_handles_hash_failure(tmp_path: Path) -> None:
    """`hash_file` raising (file vanished, permission denied, network drive
    flake) used to crash the producer thread and silently report
    `failed=0`. `_decode_one` now catches OSError and returns (sf, None,
    None) so the per-image fail counter is accurate."""
    from phototag.pipeline import _decode_one
    from phototag.scanner import ScannedFile

    # Reference a path that doesn't exist — `hash_file` will OSError on open.
    ghost = tmp_path / "ghost.jpg"
    sf = ScannedFile(path=ghost, size=0, mtime=0.0)
    result = _decode_one(sf)
    assert result == (sf, None, None)


def test_scan_and_tag_surfaces_producer_crash_in_failed(
    tmp_path: Path, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the decode producer crashes mid-stream (a non-OSError that
    `_decode_one` doesn't catch), the run summary must NOT report
    `failed=0` — the unseen-files count is accounted for in `failed`."""
    from phototag import pipeline

    _seed_corpus(tmp_path / "corpus", n=4)

    def boom(sf: object) -> object:
        raise RuntimeError("synthetic decode failure")

    monkeypatch.setattr(pipeline, "_decode_one", boom)

    store = Store(tmp_db)
    try:
        counts = pipeline.scan_and_tag(tmp_path / "corpus", store, FakeTagger(threshold=0.0))
        # Producer dies before any batch lands → seen_files = 0,
        # `failed` covers all 4 files. Tagged stays 0.
        assert counts["scanned"] == 4
        assert counts["tagged"] == 0
        assert counts["failed"] == 4
    finally:
        store.close()


def test_embed_all_surfaces_producer_crash_in_failed(
    tmp_path: Path, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symmetric to scan_and_tag's producer-crash test: if embed_all's
    decode producer dies (non-OSError escapes `_open_image`), the run
    summary must NOT report `failed=0`. Without the `decode_error`
    out-param + `seen_rows` accounting, all `len(todo)` rows would be
    silently dropped and the user would see a green run."""
    from phototag import pipeline

    _seed_corpus(tmp_path / "corpus", n=3)
    store = Store(tmp_db)
    try:
        scan_and_tag(tmp_path / "corpus", store, FakeTagger(threshold=0.0))

        def boom(_path: Path) -> None:
            raise RuntimeError("synthetic decode failure")

        monkeypatch.setattr(pipeline, "_open_image", boom)
        counts = pipeline.embed_all(store, FakeEmbedder(dim=8))
        # Producer dies on first ex.map call → seen_rows = 0; all 3
        # todo rows must surface as `failed`, embedded must stay 0.
        assert counts["total"] == 3
        assert counts["embedded"] == 0
        assert counts["failed"] == 3
    finally:
        store.close()
