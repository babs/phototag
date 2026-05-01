# 02 — Stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.14 | ML ecosystem; `pyproject.toml` `requires-python = ">=3.14"` |
| Tagger | RAM++ (`xinyu1205/recognize-anything`) | ~4500 open tags, runs locally |
| Embeddings | `open_clip` ViT-L/14 or ViT-B/32 | Standard, robust |
| Dim. reduction | `umap-learn` | Preserves local structure |
| Clustering | `hdbscan` | No `k` to fix, handles outliers |
| Inference | PyTorch + Transformers | Standard |
| Acceleration | CUDA if available, else ONNX Runtime CPU | Portability |
| EXIF / XMP | `exiftool` (subprocess) or `pyexiv2` | De facto standard |
| Storage | SQLite + JSON1 + `sqlite-vec` (optional) | Zero-dep, vectors queryable in SQL |
| CLI | `typer` | Ergonomics |
| Image hash | `xxhash` or `blake3` | Fast change detection |
| HTML reports | `jinja2` | Cluster thumbnails |
| Tests | `pytest` + 20–30 varied photo fixture | Regression guard |
| RAW decode | `rawpy` | Wide RAW support |
| HEIC decode | `pillow-heif` | iOS / modern Android |

## Why HDBSCAN over k-means

k-means forces a fixed `k`, assigns every photo to a cluster, and degrades on highly imbalanced clusters. Our case is exactly that: 5000 landscapes vs 30 medical scans. HDBSCAN finds `k` itself, marks atypical photos as noise (`cluster_id = -1`), tolerates extreme size disparity.

## Why UMAP before HDBSCAN

512-dim CLIP vectors give noisy clustering directly. UMAP → 50 dims preserves local structure (neighbors stay neighbors), improves clustering quality significantly, and enables 2D projection for visualization.

## Optional dependencies

- `sqlite-vec` — only if vector search is wanted directly in SQL. Otherwise compute cosine in Python.
- `pillow-heif`, `rawpy` — only if library contains HEIC / RAW.
- BLIP-2 — captioning, fallback when TF-IDF + CLIP zero-shot are insufficient.
