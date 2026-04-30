# 14 — Testing strategy

## Layers

| Layer | Tool | Scope |
|---|---|---|
| Unit | `pytest` | Pure functions: hash, EXIF parse, threshold filter, TF-IDF, schema migration |
| Integration | `pytest` + tmp DB | scan → tag → store → list end-to-end on fixtures |
| Property | `hypothesis` (selective) | Hash/skip idempotence, threshold monotonicity |
| Smoke | manual + CI | Full pipeline on the 20–30 photo fixture set |

## Fixtures

`tests/fixtures/` — 20–30 small JPEGs (under 200 KB each) covering:

- Landscape, portrait, indoor, food, document/screenshot
- One HEIC, one RAW (small), one PNG, one TIFF
- One corrupt file (truncated)
- One zero-byte file
- One with rich EXIF (GPS + date), one with stripped EXIF

Total fixture weight target: < 5 MB so they live in git.

## Mocking

- **RAM++ inference** — wrap behind `Tagger` Protocol. Tests inject a `FakeTagger` that returns deterministic tags. Real model exercised only in a single slow integration test gated by `@pytest.mark.slow`.
- **CLIP embeddings** — same pattern. `FakeEmbedder` returns deterministic vectors keyed by image hash.
- **GPU** — never required in CI. All tests run on CPU; real models marked `slow` and skipped by default.

## Idempotence test (load-bearing)

```python
def test_scan_is_idempotent(tmp_path, fake_tagger):
    db = Store(tmp_path / "phototag.db")
    pipeline.scan(FIXTURES, db, fake_tagger)
    snapshot1 = db.dump()
    pipeline.scan(FIXTURES, db, fake_tagger)  # second pass
    snapshot2 = db.dump()
    assert snapshot1 == snapshot2
    assert fake_tagger.call_count == len(FIXTURES)  # not 2x
```

## Migration test

Spin up DB at each prior schema version, apply migrations, assert final schema matches expected. Catches breaking schema drift early.

## Performance smoke

`tests/test_perf.py` (marked `slow`): 100-image fixture, asserts pipeline completes within a budget. Not a benchmark, just a regression guard.

## CI gates

- `ruff check` + `ruff format --check`
- `mypy --strict phototag/`
- `pytest -m "not slow"` (default)
- `pytest -m slow` nightly or manually

## Coverage

Don't chase 100%. Prioritize:

- `store.py` (DB invariants)
- `pipeline.py` (idempotence, error handling)
- `clustering.py` (param wiring)
- `xmp.py` (sidecar correctness)

CLI is exercised by integration tests via typer's `CliRunner`; no need for separate unit coverage.
