# 13 — Risks & mitigations

## Format coverage

- **HEIC / RAW** — require `pillow-heif` and `rawpy`. Test early on real samples (iOS HEIC, Canon CR2/CR3, Nikon NEF, Sony ARW). Mitigation: optional extras at install (`pip install phototag[heic,raw]`).
- **Truncated / corrupt files** — decode failures must not poison the batch. Catch + log + mark error, continue.

## Domain accuracy

- **Medical imagery** — RAM knows `x-ray`, `mri`, `medical equipment`, but is imprecise on dermato / histology. v1 clustering compensates by grouping visually similar images even without an adequate tag.
- **Niche subjects** — RAM's vocabulary is finite (~4500 tags). Things outside the bank go untagged. CLIP embeddings + clustering surface them anyway.

## Privacy

- **Faces** — RAM does not perform face recognition. v2 adds `phototag faces detect|cluster|name` via `InsightFace` (RetinaFace + ArcFace), **opt-in only**, in a separate command, gated by `--i-understand` on first run. See [`15-faces.md`](15-faces.md). Embeddings never leave the machine; `phototag faces purge` wipes everything. Never enabled by default in `scan`.
- **GPS** — extracted into `images.exif_json`. If the user shares the DB, GPS leaks. A scrub command (`phototag scrub --gps`) was planned but not shipped; for now, the workaround is `sqlite3 phototag.db "UPDATE images SET exif_json = json_remove(exif_json, '$.gps') WHERE exif_json IS NOT NULL"` before sharing.

## Idempotence

- **Critical**: `(hash, mtime)` gating prevents wasted re-tagging / re-embedding. Verified by integration test with second run on same folder asserting zero work.
- **Path moves**: hash-stable but path changes — emit `UPDATE images SET path = ?`, don't re-tag.

## Model versioning

- Store `model_name` (and ideally checksum) with every result. When RAM/CLIP version bumps, results from the old model remain valid; new model produces new rows side by side. User can choose which model to query.
- Document model versions in `pyproject.toml` extras pinning.

## Clustering stability

- HDBSCAN is not 100% deterministic; UMAP even less. Always pin `random_state`. Cluster IDs **are not stable** across runs — only the `run_id` + cluster contents are. Compare runs by Jaccard overlap of image sets, not cluster IDs.

## Licensing

- **RAM** — Apache 2.0 — confirm before redistribution.
- **open_clip** — MIT.
- **HDBSCAN** — BSD.
- **UMAP** — BSD.
- All currently compatible with permissive distribution.

## UI exposure

- **No auth on the FastAPI UI.** Endpoints serve every photo on disk by integer id. Default bind is `127.0.0.1`. Running `phototag serve --host 0.0.0.0` exposes the library + DELETE/POST mutations to the LAN; the CLI emits a `non_loopback_bind` warning on start, but the user owns the consequence past that point. CORS is restricted to localhost origins so a browser tab on another origin cannot call the API directly.

## Operational

- **Disk full mid-scan** — fail fast, no silent retry. WAL mode keeps DB consistent.
- **Concurrent CLI invocations** — SQLite `busy_timeout=5000` absorbs brief contention; past that the second writer raises `sqlite3.OperationalError: database is locked` and exits non-zero. Single-writer assumption holds; we don't try fancy multi-writer.
- **Model download on first run** — slow, network-dependent. Cache under `$XDG_CACHE_HOME/phototag/models/` (default `~/.cache/phototag/models/`; configurable via `APP_MODELS_DIR`). Models are intentionally outside the library bundle (`db_path.parent`) so per-user weights aren't duplicated alongside every library / backed up redundantly. RAM++ weights must be downloaded manually from the [recognize-anything upstream](https://github.com/xinyu1205/recognize-anything); InsightFace and open_clip auto-download on first use. A pre-warming command (`phototag models download`) was planned but not shipped — the auto-download on first inference run is sufficient in practice.

## Unknowns to resolve early

- HEIC support on the target Linux distro (libheif version).
- RAW support across the actual cameras in the user's library.
- Whether `sqlite-vec` builds cleanly on the target system; otherwise stick to numpy path.
