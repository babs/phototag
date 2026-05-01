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

## Current state

Coverage is configured in `pyproject.toml` under `[tool.coverage.run]` (branch
coverage; CLI + heavy ML modules omitted from the source set). `make test-cov`
runs term-missing, html, and xml outputs.

Test files in place:

| file | what it covers |
|---|---|
| `tests/test_cli.py` | CLI smoke (`version`, `--help`) |
| `tests/test_scanner.py` | `iter_images` extension filter, `hash_file` determinism |
| `tests/test_store.py` | core schema migrations, image upsert, tags, embeddings |
| `tests/test_store_faces.py` | face inserts, runs, clusters, identities, search by persons, group rename, cluster centroid, unassign, purge, delete-with-cluster-size-decrement |
| `tests/test_exif.py` | `_to_jsonable`, `_to_decimal`, `_parse_exif_dt`, full extract on a piexif-injected JPEG |
| `tests/test_faces_verify.py` | heuristic `verify_faces` dry run + `--apply`, threshold tuning |
| `tests/test_ui_api.py` | FastAPI TestClient over: healthz, runs, tags autocomplete, search by tag, search by person, image faces, manual face naming, group rename + split, by-name merged view, only_unnamed sidebar, delete face, drop-all-faces, corrections audit log |

Heavy paths still untested (covered by `slow` integration runs or manual):

- `phototag/models/ram.py`, `phototag/models/clip.py` — exercised only when
  `[ram]`/`[clip]` extras are installed and weights are downloaded.
- `phototag/faces.py:detect_faces_all` — needs InsightFace + cv2.
- `phototag/clustering.py`, `phototag/pipeline.py` — pending; will be added
  with `FakeTagger`/`FakeEmbedder` fixtures so they run without GPU.
- `phototag/reporting.py` — pending; needs sample image fixtures.

CI: no GitHub Actions yet. `make test-cov` is the local entry point; the
xml output is shaped for codecov.io upload when that lands.
