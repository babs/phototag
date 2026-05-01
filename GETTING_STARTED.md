# Getting started — TL;DR

Local-only, single-user. Everything runs on your machine; no telemetry,
no cloud, no auth out of the box.

## 1. Install

```sh
git clone <repo> && cd image-classifier
uv sync --extra all                  # core + RAM++ + CLIP + clustering + face + UI
uv run pre-commit install            # optional, for contributors
```

The first `phototag faces detect` triggers a one-time ~200 MB download of
InsightFace weights into `data/models/insightface/`. RAM++ weights live
under `data/models/ram_plus_swin_large_14m.pth` (download from the RAM
upstream once, ~5 GB).

## 2. Point at your photo library

The repo treats `data/pictures/` as your library. Symlink your
folder once:

```sh
ln -s /path/to/your/photos data/pictures
```

Everything `phototag` writes lives under `data/` and is gitignored.

## 3. The discovery loop (v1)

```sh
uv run phototag scan   ./data/pictures     # walks, hashes, RAM++ tags
uv run phototag embed                          # CLIP embeddings (per-image, cached)
uv run phototag cluster --min-size 20          # UMAP+HDBSCAN over CLIP
uv run phototag report --out ./report          # static HTML report
```

Idempotent: re-running `scan` is a no-op for unchanged files (gated on
`(hash, mtime)`). Pass `--force` to re-tag everything.

## 4. Browse + qualify in the UI

```sh
uv run phototag serve --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. Single-page app; press `?` for the
keyboard-shortcut overlay. Same-origin, no CORS friction.

| key | action |
|---|---|
| `←` `→` `j` `k` `Space` `PgUp` `PgDn` | navigate photos |
| `F` | toggle face overlays |
| `T` | toggle tag cloud |
| `N` | jump to next photo with an unidentified face |
| click a face → popover | name / validate / wrong / drop dups / delete |
| `V` | validate this detection (auto-advances to next un-verified named face on the photo) |
| `G` | go to this person's photos |
| `W` | wrong cluster — un-assign this face |
| `X` | drop other dups of this name on this photo |
| `D` | delete this face row (false positive) |
| `Enter` (in the name field) | save name; auto-validates |

The popover on an **unnamed** face shows top-3 identity suggestions
(cosine vs known centroids). One click attaches.

## 5. Faces workflow (opt-in, biometric)

```sh
uv run phototag faces detect --i-understand    # one-time consent gate; auto-marker thereafter
uv run phototag faces verify --apply           # heuristic cleanup (det_score < 0.65 / tiny boxes)
uv run phototag faces cluster --min-size 3     # group into people; carries forward identities
uv run phototag faces auto-attach              # bulk dry-run: attach orphans to known identities
uv run phototag faces auto-attach --persist    # commit (writes assignments + audit + auto-validates strong matches)
uv run phototag faces refine-noise             # dry-run re-cluster of noise/orphan only; --persist to save
uv run phototag faces stats                    # faces / clusters / runs / unidentified / user_verified counts
uv run phototag faces corrections              # audit log (named / unassigned / verified / unverified / deleted)
uv run phototag faces corrections-compact      # dedup the audit log per face_id (dry-run; --apply to delete)
uv run phototag faces clear-noise-labels       # one-shot recovery if you accidentally named a noise cluster
uv run phototag faces purge --yes              # nuke everything face-related
```

Manual naming via `phototag faces name CLUSTER_ID NAME` (or in the UI by
clicking a face). `--keep-identities` on `purge` keeps the identity
table so future `faces cluster` runs reattach names by centroid match.

## 6. Search + maintenance

```sh
uv run phototag list   --tag cat --tag smile           # AND across tags
uv run phototag stats  --top 50 --kind label           # corpus tag distribution (excludes geo)
uv run phototag query  "x-ray of a hand" --limit 20    # semantic search on cached CLIP embeddings
uv run phototag export --format csv --out tags.csv

uv run phototag prune  --apply                         # drop DB rows for files missing on disk
uv run phototag doctor [--fix]                         # health-check the DB (size mismatches etc.)
uv run phototag backup --out /backup/snap.db           # atomic SQLite snapshot (online, ~50 ms / 100 MB)
uv run phototag exif-backfill                          # extract EXIF for legacy rows
uv run phototag geo-tag                                # reverse-geocode GPS into city/country tags
```

## 7. Categories + XMP sidecars (digiKam / Lightroom interop)

Tags + face labels stay in the SQLite DB by default. To make them
readable in external photo apps, write them to `<image>.xmp` sidecars
next to each photo:

```sh
uv run phototag xmp write ./data/pictures --apply --include-people
```

That writes flat keywords (`dc:Subject`). For a folder-style keyword
*tree* (`medical/x-ray`, `family/Anne`, …) digiKam-style apps read
from the `lr:HierarchicalSubject` field, group your tags + face
clusters into named **categories** first:

```sh
uv run phototag category add medical
uv run phototag category map --tag x-ray --category medical
uv run phototag category map --cluster 7 --category family
uv run phototag category list
```

Or do all of that point-and-click in the UI: the sidebar's
**categories** view (third tab) lists rules, lets you add/remove
them, and binds tags via an autocomplete sourced from your live tag
set. The next `xmp write --apply` materializes
`category|subject` entries in every applicable sidecar.
Spec: [`specs/08-xmp-categories.md`](specs/08-xmp-categories.md).

## 8. Optional: shared-secret auth (when binding non-loopback)

The default `--host 127.0.0.1` is auth-free. Binding to LAN
(`--host 0.0.0.0`) exposes every photo by integer id; gate it with a
token:

```sh
APP_API_TOKEN=somethingsecret uv run phototag serve --host 0.0.0.0 --port 8090
```

Or rotate without restart:

```sh
echo -n "rotated-secret" > /run/phototag.token
APP_API_TOKEN_FILE=/run/phototag.token uv run phototag serve --host 0.0.0.0 --port 8090
# later: `echo -n "newer-secret" > /run/phototag.token` — middleware re-reads per request.
```

The middleware uses `secrets.compare_digest`. CORS preflight passes
through. The SPA injects the token automatically into fetch + asset
URLs from the rendered template.

## 9. Daily-use cheat sheet

```sh
# I added new photos to the library
uv run phototag scan ./data/pictures && uv run phototag embed

# I want to re-cluster after qualifying a chunk of faces
uv run phototag faces refine-noise --persist           # picks up named identities, leaves named clusters alone
# or, to attach orphans without touching cluster topology:
uv run phototag faces auto-attach --persist

# Snapshot the DB before something destructive
uv run phototag backup --out data/backups/$(date -u +%Y%m%dT%H%M%S).db

# Catch DB drift
uv run phototag doctor --fix

# Clean up missing files
uv run phototag prune --apply
```

## 10. Where things live

- `data/` — gitignored; DBs (`*.db`), model weights, EXIF cache, thumbs,
  preview JPEGs, face thumbnails, server logs, backups.
- `phototag.db` (or `data/full.db` via `APP_DB_PATH`) — single SQLite
  file, WAL-mode, schema migrations are numbered + atomic.
- `report*/` — generated HTML reports.
- `specs/` — design + roadmap; `specs/16-improvement-plan.md` tracks the
  forward-looking backlog (🟢 shipped / ⬜ pending).
- `static/src/` — ESM source for the UI; bundled to `static/ui.js` via
  esbuild. After editing JS, run `make js-build` (one-time `npm install`
  first to fetch esbuild). Bundle output is committed; `node_modules/` is
  gitignored.

## 11. Configuration

`.env` overrides (`APP_` prefix, pydantic-settings):

| var | default | what |
|---|---|---|
| `APP_LOG_LEVEL` | `INFO` | structlog level |
| `APP_JSON_LOGS` | auto | force json/console; auto = TTY detect |
| `APP_DB_PATH` | `phototag.db` | SQLite file |
| `APP_MODELS_DIR` | `data/models` | weights cache |
| `APP_DEVICE` | `auto` | `auto` / `cpu` / `cuda` |
| `APP_API_TOKEN` | (unset) | static shared secret for the UI |
| `APP_API_TOKEN_FILE` | (unset) | path to a token file (re-read per request) |

## Pointers

- Architecture / data model / roadmap: `specs/`
- Faces design + privacy contract: `specs/15-faces.md`
- Improvement backlog (status + effort): `specs/16-improvement-plan.md`
- Project conventions for contributors: `CLAUDE.md`
