# phototag

Local photo tagging and clustering. Scans a folder, tags each image with RAM++, embeds with CLIP, clusters with UMAP+HDBSCAN, produces a navigable HTML report.

**New here? Read [`GETTING_STARTED.md`](GETTING_STARTED.md) — a TL;DR of install, daily commands, UI shortcuts.**

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
uv run phototag faces auto-attach                    # bulk identity match for orphans (dry-run; --persist to save)
uv run phototag faces stats
uv run phototag faces corrections --action named     # audit log dump
uv run phototag faces clear-noise-labels             # recover from naming-noise bug
uv run phototag faces purge [--keep-identities] --yes
```

UI per-face shortcuts: V validate, G go-to-person, W wrong (un-cluster),
D delete, X drop dups of this name. Per-image actions: bulk validate
named faces, drop unidentified, re-detect (preserves validated faces).
`?` opens the keyboard-shortcut help overlay.

### v1.5 search & maintenance

```sh
uv run phototag list --tag cat --tag smile           # AND across tags
uv run phototag stats --top 50 --kind label          # excludes geo
uv run phototag query "x-ray of a hand" --limit 20   # CLIP semantic search
uv run phototag export --format csv --out tags.csv
uv run phototag prune --apply                        # drop rows for missing files
uv run phototag doctor [--fix]                       # health-check the DB; --fix recomputes safe items
```

### Optional auth (when binding non-loopback)

```sh
APP_API_TOKEN=somethingsecret uv run phototag serve --host 0.0.0.0 --port 8090
```

When `APP_API_TOKEN` is set, every API request requires
`X-API-Token: <value>` header (or `?token=<value>` query for native
asset loads); the SPA wires this automatically from the rendered template.
Empty / unset disables auth — fine for the localhost-only default.

To rotate the token without restarting the server, point `APP_API_TOKEN_FILE`
at a small file holding the token; the middleware re-reads it on every
protected request, so editing the file takes effect on the next call.

```sh
echo -n "rotated-secret" > /run/phototag.token
APP_API_TOKEN_FILE=/run/phototag.token uv run phototag serve --host 0.0.0.0 --port 8090
# later: `echo -n "newer-secret" > /run/phototag.token` — no restart needed.
```

## License

Apache-2.0
