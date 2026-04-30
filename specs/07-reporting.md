# 07 — HTML report (v1)

Static HTML output, browsable offline. One report per clustering run.

## Pages

### Index page

- 2D UMAP projection (scatter plot, colored by cluster). Pre-rendered PNG/SVG via matplotlib, no JS.
- Global stats: total images, cluster count, noise size, mean/median/min/max cluster sizes.
- Table of clusters with: id, auto-label, suggested CLIP label, size, link to detail page.

### Per-cluster page

- Cluster id, auto-label, CLIP-suggested label, size.
- Top TF-IDF tags (e.g., 10 dominant).
- Editable user-label field (textarea + JS that POSTs to local API, or generates a JSON snippet to paste back).
- Grid of 25 thumbnails closest to centroid (256 px JPEG, generated once and cached under `report_assets/`).
- Click thumbnail → opens original path in OS default app (`file://` link).

## Generation

- Jinja2 templates under `templates/`.
- Output: `report/<run_id>/index.html` + `report/<run_id>/cluster_<id>.html` + `report/<run_id>/thumbs/`.
- Thumbnails generated lazily; reuse across runs when image hash unchanged.

## Renaming clusters

Two modes:

- **Static** — copy displayed JSON `{cluster_id: user_label, ...}` and run `phototag cluster rename --from rename.json`.
- **Live** (optional) — `phototag report --serve` runs FastAPI locally, textarea POSTs persist `clusters.label_user` immediately.

Static is enough for v1; live mode is a later stretch.

## Thumbnail strategy

- Square crop, center, 256 px max side.
- JPEG quality 80.
- File name: `<sha256(path):16>.jpg` so it survives library reorganization.
- Stored under `report_assets/thumbs/`, not in DB.
