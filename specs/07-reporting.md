# 07 — HTML report (v1)

Static HTML output, browsable offline. One report per clustering run.

## Pages

### Index page

- Global stats: total images, cluster count, noise size, per-cluster sizes (read from `clusters.size`).
- Table of clusters with: id, cluster_no, auto-label, user-label, size, link to detail page.
- A 2D UMAP projection was originally planned but not shipped — `report` is JS-free static HTML; the FastAPI UI (`phototag serve`) is the interactive surface. Adding a pre-rendered matplotlib scatter is straightforward if the static HTML view becomes worth investing in.

### Per-cluster page

- Cluster id, auto-label (TF-IDF), user-label, cluster_no, size.
- Grid of up to 25 thumbnails (`THUMB_PER_CLUSTER`), 256 px max side, JPEG q80, EXIF-orientation baked in. Thumbs are content-hashed so renames don't trigger regeneration.
- Each thumbnail links to the original file via `file://` URI.

## Generation

- Jinja2 templates: `templates/index.html.j2`, `templates/cluster.html.j2`.
- Output: `<out>/index.html` + `<out>/cluster_<id>.html` + `<out>/thumbs/<sha256(path):16>.jpg` + `<out>/data.json` (machine-readable copy).
- Default `--out report` (under cwd); `--run-id N` overrides "latest cluster_run".
- Thumbnails are written once per content hash; re-running `report` reuses them.

## Renaming clusters

Two CLI commands (no built-in editor in the static HTML — that lives in the FastAPI UI):

- `phototag rename CLUSTER_ID [LABEL]` — set or clear `label_user` on one cluster (omit `LABEL` to clear).
- `phototag rename-bulk JSON_PATH` — apply `{cluster_id: label_user}` from a JSON file in one transaction.

For interactive editing, run `phototag serve` and use the cluster pane (✏️ button on each cluster).

## Thumbnail strategy

- Square crop, center, 256 px max side.
- JPEG quality 80.
- File name: `<sha256(path):16>.jpg` so it survives library reorganization.
- Stored under `report_assets/thumbs/`, not in DB.
