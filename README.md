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

## License

Apache-2.0
