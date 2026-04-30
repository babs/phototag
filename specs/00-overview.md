# 00 — Overview

## Goal

Local, open-source, scriptable tool that scans a photo library and produces, for each image, a list of open-vocabulary tags via **RAM++ (Recognize Anything Model Plus)**. Purpose: explore real library content without presupposing categories, then derive a taxonomy *a posteriori* by clustering.

## Non-goals

- Not a photo manager UI (digiKam, PhotoPrism already exist).
- Not face recognition (separate concern, opt-in only — see `13-risks.md`).
- Not cloud-dependent. Runs offline.

## Versions

The corpus content is unknown a priori, so the v1 deliverable is the *full discovery loop*: scan → tag → embed → cluster → HTML report. Tag filtering and search are useful only once the user has a mental map of what's in there, so they ship as v1.5.

### v1 — Discovery loop (core deliverable)

- Recursive folder scan
- RAM++ inference, local (CPU or GPU)
- CLIP embeddings per image
- UMAP + HDBSCAN clustering
- Automatic cluster naming (TF-IDF on RAM tags + CLIP zero-shot)
- HTML report with thumbnails to validate / rename
- Simple CLI

### v1.5 — Polish & search

- Semantic search (`--query "medical"`)
- `list` / `stats` / `export` CLI commands
- EXIF extraction (date / GPS / camera)
- Cluster rename workflow

### v2 — Productivity

- XMP sidecar writes (portable tags, read by digiKam, Lightroom, …)
- Tags + clusters → user-defined categories mapping
- Minimal web UI (FastAPI + HTMX or Streamlit)

## Recommended starting point

Build straight to v1 against a 500–1000 photo representative sample. The HTML report is the validation artifact: open it, browse clusters, decide whether RAM tagging quality and cluster shapes match expectations before investing in v1.5/v2.
