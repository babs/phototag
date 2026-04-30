# 01 — Architecture

## Component diagram

```
[Scanner] ─→ [Queue] ─┬─→ [Worker RAM]   ─┐
                      └─→ [Worker CLIP]  ─┴─→ [Store SQLite] ─→ [API/CLI/Export]
                                                       │
                                                       ├─→ [Clusterer UMAP+HDBSCAN] (v1)
                                                       └─→ [XMP writer]            (v2)
```

## Components

| Component | Role | Stage |
|---|---|---|
| Scanner | Recursive walk, extension filter, hash + mtime | v1 |
| Queue | Decouples I/O from inference; batches | v1 |
| Worker RAM | RAM++ inference, batch GPU/CPU | v1 |
| Worker CLIP | open_clip embeddings | v1 |
| Store | SQLite (file-based, portable) | v1 |
| Clusterer | UMAP → HDBSCAN → naming | v1 |
| Reporter | HTML + thumbnails | v1 |
| Search | Cosine similarity over CLIP vectors | v1.5 |
| XMP writer | sidecar via exiftool/pyexiv2 | v2 |

## Decoupling principle

Each model sits behind an interface (`Tagger`, `Embedder`). Swapping RAM → BLIP or CLIP → SigLIP must not require touching scanner, store, CLI, or reporting. See `phototag/models/base.py` (planned, see `10-project-structure.md`).

## Data flow

1. Scanner emits `(path, hash, mtime)` tuples.
2. Store deduplicates: skip if `(hash, mtime)` already processed.
3. Workers consume in batches (8–32) for GPU efficiency.
4. Tags + embeddings + EXIF persisted in single transaction per batch.
5. Clusterer reads embeddings, writes cluster assignments.
6. Reporter / Search / Export read-only consumers of the store.

## Concurrency model

- I/O bound work (scan, decode, hash): multiprocess pool.
- GPU inference: single process, batched. GPU is the bottleneck — no benefit splitting it.
- CLI / report: synchronous, single-process.
