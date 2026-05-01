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
coverage; `phototag/__init__.py`, `phototag/models/ram.py`, `phototag/models/clip.py`
omitted from the source set because they're heavy ML wrappers exercised only
behind `slow` integration). `make test-cov` runs term-missing, html, and xml
outputs.

**83 tests** in place across 7 files (was 42 at v1):

| file | what it covers |
|---|---|
| `tests/test_cli.py` | `version`, `--help`, `prune` dry-run + `--apply`, `list --tag`, `stats --kind`, `export json/csv` round-trip, `doctor` size-mismatch detect + `--fix`, `backup` atomic snapshot, `category add/rm/list/map/unmap` CLI round-trip, `faces corrections-compact` |
| `tests/test_scanner.py` | `iter_images` extension filter, `hash_file` determinism |
| `tests/test_store.py` | schema migrations, image upsert, tag round-trip, embedding round-trip, `delete_image` cascade through tags / faces / embeddings |
| `tests/test_store_faces.py` | face inserts / runs / clusters / identities, search by persons, group rename, cluster centroid, unassign, purge, delete-with-cluster-size-decrement, `attach_face_to_best_identity` (success / no-match / dim-mismatch / margin / cannot-link / noise-detach), `auto_attach_orphans` (dry-run + persist + image_id-in-audit + cannot-link), edge gallery, **per-identity threshold (Welford folder, threshold widening, attach-records-sim)**, **tier-3 constraint edge derivation + matrix surgery**, **categories union + cluster/tag rule cascade** |
| `tests/test_exif.py` | `_to_jsonable`, `_to_decimal`, `_parse_exif_dt`, full extract on a piexif-injected JPEG |
| `tests/test_faces_verify.py` | heuristic `verify_faces` dry-run + `--apply`, threshold tuning |
| `tests/test_xmp.py` | (skips when `exiftool` not installed) `write_sidecar` round-trip + idempotence + rewrite-on-content-change, `clean_sidecar`, `phototag xmp write/clean` CLI dry-run + `--apply`, `lr:HierarchicalSubject` emission for tag→category and cluster→category rules |
| `tests/test_ui_api.py` | FastAPI `TestClient` — every endpoint: healthz, runs, tags autocomplete, search by tag / person, image faces, manual face naming (verify + auto-detach + cannot-link), group rename + split + merge, by-name merged view + edge view, only_unnamed sidebar, validate-named bulk + drop-dups, redetect IoU preservation, delete face, drop-all + drop-unidentified, corrections audit log, suggest top-K, lib-wide drop yes-required, rename skips noise, triage queue, `APP_API_TOKEN[_FILE]` middleware (constant-time compare + hot rotation), **categories CRUD + tag/cluster rule round-trip + 404 on unknown targets**, **redraw-bbox validation paths (face/image/file 404, malformed-bbox 400, too-small 422)** |

Heavy paths still untested (covered by `slow` integration runs or manual):

- `phototag/models/ram.py`, `phototag/models/clip.py` — exercised only when
  `[ram]` / `[clip]` extras are installed and weights are downloaded.
- `phototag/faces.py:FaceDetector.detect` (RetinaFace + ArcFace inference) — needs InsightFace + onnxruntime.
- `phototag/faces.py:cluster_faces` and `cluster_orphan_faces` — UMAP + HDBSCAN inside; `auto_attach_orphans` and `apply_sticky_corrections` are tested directly. A `FakeTagger` / `FakeEmbedder` fixture set is the natural way to exercise `pipeline.scan_and_tag` / `pipeline.embed_all` end-to-end without GPU; not yet built (escalation #L10 in the prior reviews; #6 in `16-improvement-plan.md` group "Performance / correctness" → backlog).
- `phototag/reporting.py` — pending; needs sample image fixtures.
- `phototag/pipeline.py` (orchestration) — same fixture story as clustering.

CI: shipped — `.github/workflows/ci.yml`. Two jobs:

- **`fast`** (push / PR): `uv sync --extra ui --group dev`, then the
  project pre-commit stack (ruff lint + format + mypy + secrets +
  pyupgrade) followed by `pytest -m "not slow" --cov=phototag`.
- **`slow`** (cron `17 3 * * *`): `uv sync --extra all --group dev`,
  then `pytest -m slow` with a 60-min timeout. Tolerates pytest exit
  code 5 (no slow tests yet) so the nightly stays green until model-
  backed tests are added.

`make test-cov` remains the local entry point; coverage output is shaped
for codecov.io upload (not yet wired). Verified locally with
`act -j fast` / `act schedule -j slow`.
