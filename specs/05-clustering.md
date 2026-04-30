# 05 — Clustering & category discovery (v1)

Exploratory phase that turns the mass of tags into a usable taxonomy.

## Pipeline

1. **CLIP embeddings** — one 512- or 768-dim vector per image, computed once and cached in DB. Reused for semantic search.
2. **UMAP reduction** — 512 → 50 dims. Preserves local structure (neighbors stay neighbors), improves clustering quality, enables 2D projection for visualization.
3. **HDBSCAN** — finds cluster count itself, marks atypical photos as noise (`cluster_id = -1`), tolerates very uneven sizes (5000 landscapes vs 30 medical scans). Key parameter: `min_cluster_size` (5–50 depending on volume).

Why not k-means: forces fixed `k`, forces every photo into a cluster, behaves poorly with imbalanced clusters — exactly our case.

## Parameters

| Component | Param | Default | Note |
|---|---|---|---|
| UMAP | `n_components` | 50 | clustering input |
| UMAP | `n_neighbors` | 30 | smoothness vs detail |
| UMAP | `min_dist` | 0.0 | tight clusters |
| UMAP | `metric` | `cosine` | matches CLIP geometry |
| UMAP | `random_state` | 42 | reproducibility |
| HDBSCAN | `min_cluster_size` | 20 | tune with library size |
| HDBSCAN | `min_samples` | 5 | conservative noise marking |
| HDBSCAN | `metric` | `euclidean` | post-UMAP |
| HDBSCAN | `cluster_selection_method` | `eom` | excess of mass |

Persist params + seeds in `cluster_runs.params_json` (see `03-data-model.md`).

## Automatic cluster naming

Four methods combined, ordered by usefulness:

| # | Method | Principle | Cost | Output |
|---|---|---|---|---|
| A | **TF-IDF on RAM tags** | Tags frequent in cluster AND rare elsewhere | ~zero | `x-ray, bone, radiograph` |
| B | **CLIP zero-shot on centroid** | Score centroid against candidate label bank | low | suggested main category |
| C | **Visual inspection** | Grid of N photos closest to centroid | ~30 s/cluster, manual | final validation |
| D | **BLIP-2 caption** (option) | Free description on 3–5 central photos | heavier | descriptive sentence |

**A is the core**: leverages already-computed RAM tags, zero extra inference. B and C validate. D only if A+B insufficient.

### Candidate label bank for B

Curated list of ~100 labels covering expected high-level categories: `landscape, portrait, food, document, screenshot, x-ray, mri, ultrasound, plant, animal, vehicle, building, …`. User-extendable via `--labels labels.txt`.

## Output

HTML report per clustering run (see `07-reporting.md`).

## Re-clustering

When library grows, re-cluster periodically. Keeping `image → previous_cluster_label` mapping detects drift (clusters splitting or merging). Each run gets a new `run_id`; old runs stay in DB for diff.

## Stability caveat

HDBSCAN is not 100% deterministic; UMAP even less. Always pin `random_state`. Use `run_id` for run-to-run comparison rather than expecting cluster IDs to remain stable.

## Volume expectations

On 100k photos: typically 30–200 useful clusters + one noise cluster. Manual validation: 1–2 hours.
