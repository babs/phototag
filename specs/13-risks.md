# 13 — Risks & mitigations

## Format coverage

- **HEIC / RAW** — require `pillow-heif` and `rawpy`. Test early on real samples (iOS HEIC, Canon CR2/CR3, Nikon NEF, Sony ARW). Mitigation: optional extras at install (`pip install phototag[heic,raw]`).
- **Truncated / corrupt files** — decode failures must not poison the batch. Catch + log + mark error, continue.

## Domain accuracy

- **Medical imagery** — RAM knows `x-ray`, `mri`, `medical equipment`, but is imprecise on dermato / histology. v1 clustering compensates by grouping visually similar images even without an adequate tag.
- **Niche subjects** — RAM's vocabulary is finite (~4500 tags). Things outside the bank go untagged. CLIP embeddings + clustering surface them anyway.

## Privacy

- **Faces** — RAM does not perform face recognition. v2 adds `phototag faces detect|cluster|name` via `InsightFace` (RetinaFace + ArcFace), **opt-in only**, in a separate command, gated by `--i-understand` on first run. See [`15-faces.md`](15-faces.md). Embeddings never leave the machine; `phototag faces purge` wipes everything. Never enabled by default in `scan`.
- **GPS** — extracted into `images.exif_json`. If the user shares the DB, GPS leaks. Provide `phototag scrub --gps` to drop GPS fields.

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
- **Concurrent CLI invocations** — DB lock. Detect, exit code 10. Don't try fancy multi-writer.
- **Model download on first run** — slow, network-dependent. Cache under `data/models/`. Provide `phototag models download` for pre-warming.

## Unknowns to resolve early

- HEIC support on the target Linux distro (libheif version).
- RAW support across the actual cameras in the user's library.
- Whether `sqlite-vec` builds cleanly on the target system; otherwise stick to numpy path.
