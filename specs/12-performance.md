# 12 — Performance targets

## Throughput

| Hardware | RAM++ tagging | CLIP embeddings |
|---|---|---|
| GPU consumer (RTX 3060+) | 30–80 img/s | 100–200 img/s |
| CPU modern (8 cores) | 1–3 img/s | 5–10 img/s |

## Memory

| Resource | Footprint |
|---|---|
| RAM++ weights on disk | ~5 GB |
| RAM++ VRAM | 3–4 GB |
| CLIP ViT-L/14 VRAM | ~1.5 GB |
| CLIP ViT-B/32 VRAM | ~600 MB |
| 100k embeddings (ViT-L) | ~300 MB DB / ~300 MB RAM at load |

## Clustering throughput

| Step | 100k vectors |
|---|---|
| UMAP fit + transform | 2–5 min |
| HDBSCAN | < 1 min |
| TF-IDF naming | < 30 s |

## End-to-end (100k photos)

| Phase | GPU | CPU |
|---|---|---|
| Scan + tag | ~30 min | ~10–15 h |
| Embed (CLIP) | ~10 min | ~3 h |
| Cluster + name + report | ~5–10 min | ~5–10 min |
| **Total v1 first run** | **~45 min** | **~12–18 h** |

## Targets

- **No memory ceiling on library size**: streaming pipeline, never load all images at once.
- **Resumable**: a `Ctrl-C` mid-scan must not lose committed progress. Per-batch transactions enforce this.
- **GPU saturation > 80%** when batch size auto-tunes correctly. Monitor via `nvidia-smi` during dev.
- **DB write contention**: single writer process, batch transactions. WAL mode.

## Pragmatics

- First-run cost dominates. Re-runs are near-free thanks to `(hash, mtime)` skip.
- CLIP embeddings only computed once per image-model pair; cluster runs read from DB.
- Don't optimize past these targets without profiling on the actual library.
