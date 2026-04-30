# 13 — Risks & mitigations

## Format coverage

- **HEIC / RAW** — require `pillow-heif` and `rawpy`. Test early on real samples (iOS HEIC, Canon CR2/CR3, Nikon NEF, Sony ARW). Mitigation: optional extras at install (`pip install phototag[heic,raw]`).
- **Truncated / corrupt files** — decode failures must not poison the batch. Catch + log + mark error, continue.

## Domain accuracy

- **Medical imagery** — RAM knows `x-ray`, `mri`, `medical equipment`, but is imprecise on dermato / histology. v1 clustering compensates by grouping visually similar images even without an adequate tag.
- **Niche subjects** — RAM's vocabulary is finite (~4500 tags). Things outside the bank go untagged. CLIP embeddings + clustering surface them anyway.

## Privacy

- **Faces** — RAM does not perform face recognition. If needed later: integrate `InsightFace` or similar, **opt-in only**, in a separate command. Never on by default. Don't store face embeddings without explicit user action.
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

## Operational

- **Disk full mid-scan** — fail fast, no silent retry. WAL mode keeps DB consistent.
- **Concurrent CLI invocations** — DB lock. Detect, exit code 10. Don't try fancy multi-writer.
- **Model download on first run** — slow, network-dependent. Cache under `data/models/`. Provide `phototag models download` for pre-warming.

## Unknowns to resolve early

- HEIC support on the target Linux distro (libheif version).
- RAW support across the actual cameras in the user's library.
- Whether `sqlite-vec` builds cleanly on the target system; otherwise stick to numpy path.
