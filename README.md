# phototag

Local photo tagging and clustering. Scans a folder, tags each image with RAM++, embeds with CLIP, clusters with UMAP+HDBSCAN, produces a navigable HTML report.

See [`specs/`](specs/) for the design.

## Install

Core (no models):

```sh
uv sync
```

With all ML extras (RAM++, CLIP, clustering, report, HEIC, RAW, EXIF, UI):

```sh
uv sync --extra all
```

Activate hooks:

```sh
uv run pre-commit install
```

## Quickstart

```sh
uv run phototag scan ./data/photo-corpus
uv run phototag embed ./data/photo-corpus
uv run phototag cluster --min-size 20
uv run phototag report --out ./report
```

### Browse, search, qualify

```sh
uv run phototag serve --host 127.0.0.1 --port 8000
```

Opens a single-page UI for browsing clusters, searching by tag / person,
inspecting a photo (EXIF, GPS, original link) and managing face overlays
in the lightbox.

### Faces (opt-in, biometric — see [`specs/15-faces.md`](specs/15-faces.md))

```sh
uv run phototag faces detect --i-understand          # one-time consent gate
uv run phototag faces cluster --min-size 3
uv run phototag faces verify --apply                 # heuristic cleanup
uv run phototag faces refine-noise                   # dry-run by default; --persist to save
uv run phototag faces stats
uv run phototag faces clear-noise-labels             # recover from naming-noise bug
uv run phototag faces purge [--keep-identities] --yes
```

UI per-face shortcuts: V validate, G go-to-person, W wrong (un-cluster),
D delete, X drop dups of this name. Per-image actions: bulk validate
named faces, drop unidentified, re-detect (preserves validated faces).

## License

Apache-2.0
