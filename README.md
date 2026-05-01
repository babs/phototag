# phototag

Local, single-user photo-tagging and face-management tool. Scans a folder,
tags every image with **RAM++** (~4500 open-vocabulary labels), embeds with
**CLIP** for semantic search, clusters with **UMAP + HDBSCAN** to surface
the corpus's natural taxonomy, optionally detects + clusters **faces** via
RetinaFace + ArcFace, and exposes everything through a CLI plus a
single-page FastAPI UI.

Runs offline. No telemetry. No cloud. Your photos and embeddings never
leave your machine.

> **New here?** Start with [`GETTING_STARTED.md`](GETTING_STARTED.md) — a
> TL;DR install + daily-commands + UI shortcuts walkthrough.

## Why

Existing photo managers (digiKam, PhotoPrism, Apple/Google Photos) ask
you to invent your own categories before they can help. RAM++ + CLIP
flip that around: tag everything with an open vocabulary, then let the
clustering surface the categories the corpus actually contains. You see
"x-ray scans, vacation landscapes, screenshots, kids portraits" without
having defined any of those buckets.

Faces are a parallel track: detect → embed → cluster → name → browse.
Strictly opt-in (`--i-understand` on first run), strictly local, the
embeddings never leave the machine. Wipeable in one command.

## Status

- **v1 — Discovery loop**: shipped (scan, embed, cluster, HTML report).
- **v1.5 — Search & maintenance**: shipped (`query`, `list`, `stats`,
  `export`, `prune`, `doctor`, `backup`, EXIF + reverse-geocoding).
- **v2 — Productivity**: faces shipped (detect, cluster, validate,
  identity merge, hard-negative cannot-link, sticky corrections,
  triage queue, edge gallery, vectorized auto-attach). XMP sidecars
  and user-defined categories still pending.

Live status table: [`specs/16-improvement-plan.md`](specs/16-improvement-plan.md).

## What's in the box

### Scan & tag

- Recursive walk; xxhash64 + mtime gating so re-runs are no-ops on
  unchanged files.
- RAM++ batched inference (GPU or CPU); per-tag scores stored so
  thresholds can be re-applied without re-running.
- EXIF extraction (date, camera, exposure, GPS) into a JSON field.
- Reverse-geocode GPS into city/country tags via offline cities-1000.

### Embed, cluster, report

- CLIP image embeddings cached in SQLite; reused for clustering and
  semantic search.
- UMAP → HDBSCAN; cluster labels via TF-IDF on RAM tags.
- Static HTML report with per-cluster thumbnails.
- Re-cluster anytime; runs are versioned.

### Search

- `phototag query "TEXT"` — CLIP semantic search over cached embeddings.
- `phototag list --tag X --tag Y` — AND across tags with score floor.
- `phototag stats --kind label` — corpus tag distribution; geo facts
  separated from model labels.
- `phototag export --format csv|json` — bulk dump.

### Faces (opt-in, biometric)

- RetinaFace detection + ArcFace embedding (InsightFace `buffalo_l`).
- UMAP + HDBSCAN clustering; identity carry-forward via Hungarian
  assignment + sample-weighted centroid blend (capped so identities
  drift on new evidence).
- Per-face validate / wrong / drop-dups / delete / re-detect.
- **Cannot-link** from `face_corrections` — once you say "wrong" on a
  face, the system never re-suggests that identity for that face.
- **Triage queue** — finite walk through photos with unverified named
  faces or duplicate-name overlays.
- **Fringe view** — per identity, the 9 most-uncertain faces (cluster
  edge), one-click verify or unassign.
- **Bulk auto-attach** — vectorized cosine match of every orphan face
  against every identity centroid in one matmul.
- **Identity merge** — collapse two identity rows into one, blends
  centroids by sample count.
- **Re-cluster orphans only** — `phototag faces refine-noise` on the
  noise/orphan pool; named clusters never touched.

### UI

- FastAPI single-page app, same-origin (no CORS friction).
- Lightbox with face overlays (per-cluster colour, ⚠ for duplicate
  names, `?` for unverified, sim badge for auto-attached, ✓ implicit
  for validated).
- Sidebar with cluster filter (space-delimited AND tokens, bold
  matches, X clear), pinned **noise / orphan** + **triage queue**
  entries.
- Keyboard everywhere: `?` opens a help overlay listing every shortcut.
- Optional `APP_API_TOKEN[_FILE]` middleware for non-loopback binds
  (constant-time compare, hot-rotation via file watch).

### Operations

- `phototag prune [--apply]` — drop DB rows for files missing on disk.
- `phototag doctor [--fix]` — health check (size mismatches, orphan
  identities, schema-version drift).
- `phototag backup --out PATH` — atomic SQLite snapshot.
- `phototag faces corrections-compact` — dedup the audit log per
  face_id.
- `phototag faces clear-noise-labels` — recovery for a historic bug
  where naming the noise cluster mass-tagged its members.

## Install

```sh
git clone <repo> && cd image-classifier
uv sync --extra all                     # core + RAM++ + CLIP + clustering + faces + UI
uv run pre-commit install               # contributors only
```

Heavy ML deps live behind extras (`[ram]`, `[clip]`, `[cluster]`,
`[face]`, `[heic]`, `[raw]`, `[exif]`, `[geo]`, `[ui]`, `[report]`).
Core install (`uv sync` without extras) works without any model.

Weights:
- RAM++ (~5 GB) — download `ram_plus_swin_large_14m.pth` from the
  [recognize-anything](https://github.com/xinyu1205/recognize-anything)
  upstream into `data/models/`.
- InsightFace buffalo_l (~200 MB) — auto-downloads on first
  `phototag faces detect`.
- open_clip ViT-B/32 — auto-downloads on first `phototag embed`.

## Quick start

See [`GETTING_STARTED.md`](GETTING_STARTED.md) for the full TL;DR. The
30-second version:

```sh
ln -s /path/to/your/photos data/photo-corpus       # point at your library

uv run phototag scan ./data/photo-corpus           # tag with RAM++
uv run phototag embed                              # CLIP embeddings
uv run phototag cluster --min-size 20              # UMAP + HDBSCAN
uv run phototag report --out ./report              # static HTML
uv run phototag serve --port 8000                  # interactive UI
```

Faces (opt-in):

```sh
uv run phototag faces detect --i-understand        # consent gate (one-time)
uv run phototag faces cluster --min-size 3         # group into people
uv run phototag faces auto-attach --persist        # bulk-attach orphans
uv run phototag faces stats                        # see counts
```

## Architecture

```
[Scanner] -> [Queue] -+-> [Worker RAM]   --+
                      +-> [Worker CLIP]   -+-> [Store SQLite] -> [API/CLI/Export]
                                                    |
                                                    +-> [Clusterer UMAP+HDBSCAN]
                                                    +-> [Faces detect/cluster]
                                                    +-> [HTML report]
```

Single SQLite file (WAL mode, atomic numbered migrations,
thread-local connections + write lock for the FastAPI threadpool).
Models behind `Tagger` / `Embedder` / `FaceDetector` interfaces so
swapping a backend doesn't touch the rest of the pipeline.

Detail: [`specs/01-architecture.md`](specs/01-architecture.md),
[`specs/03-data-model.md`](specs/03-data-model.md),
[`specs/15-faces.md`](specs/15-faces.md).

## Privacy

This tool processes biometric data when face features are enabled.
Hard rules baked in:

1. **Opt-in**: `phototag scan` never triggers detection. Face commands
   require `--i-understand` on first run.
2. **Local only**: every embedding stays in the SQLite file under
   `data/`. No network calls during inference.
3. **Wipeable**: `phototag faces purge --yes` drops every face row,
   cluster, identity, and audit-log entry. `--keep-identities` keeps
   the names but drops the embeddings.
4. **Don't process other people's libraries** without their consent.

GPS data is extracted into `images.exif_json`; if you share the DB,
GPS leaks. Sanitize before exporting.

Full statement: [`specs/15-faces.md`](specs/15-faces.md) §"Privacy &
ethics".

## Tech stack

- **Python 3.14** (uv-managed; modern type hints throughout).
- **SQLite** (WAL, JSON1, single-file portable).
- **FastAPI + uvicorn** for the UI; same-origin SPA, no bundler.
- **PyTorch + Transformers** for RAM++; **open_clip** for CLIP;
  **InsightFace + onnxruntime** for faces.
- **UMAP + HDBSCAN** for clustering; **scipy** for Hungarian
  assignment.
- **structlog** with TTY detection; **typer** for the CLI;
  **pydantic-settings** for config; **xxhash** for content hashing.
- **pytest** for testing (83+ tests; CLI / Store / API / face
  helpers covered; heavy ML paths exercised by slow integration
  marker).
- **ruff** + **mypy --strict** + **pre-commit** for the contributor
  loop.

## Project layout

```
phototag/
  cli.py            typer entry point — every command
  pipeline.py       scan + tag + embed orchestration (batched)
  scanner.py        recursive walk + xxhash + mtime
  store.py          SQLite wrapper (migrations, thread-local conns,
                    write lock, all queries)
  exif.py           EXIF extraction + sanitization
  geo.py            offline reverse-geocoding (cities-1000)
  clustering.py     UMAP + HDBSCAN + TF-IDF cluster naming
  reporting.py      static HTML report (Jinja2)
  faces.py          face detect, cluster, identity match, sticky
                    corrections, attach, refine-noise, auto-attach
  ui.py             FastAPI app + every endpoint
  models/
    base.py         Tagger / Embedder Protocols
    ram.py          RAM++ wrapper
    clip.py         open_clip wrapper
  logging.py
  config.py
  settings.py       APP_* env-var bound via pydantic-settings

static/             ui.css + ui.js (esbuild bundle of static/src/*.js)
static/src/         ESM modules — state, api, lightbox, sidebar,
                    workspace, keyboard, runs, main
templates/          ui.html, cluster.html.j2, index.html.j2
specs/              design + roadmap + improvement plan
tests/              pytest suites (CLI / Store / API / faces / EXIF)
data/               gitignored: DB, model weights, caches, backups
```

## Development

```sh
make lint                  # pre-commit on all files
uv run pytest              # fast tests (default)
uv run pytest -m slow      # tests requiring downloaded models
make test-cov              # term-missing + html + xml
make js-build              # bundle static/src/*.js -> static/ui.js
make js-watch              # same, in watch mode
```

The frontend lives in `static/src/` as ESM modules and is bundled to a
single ES2020 IIFE at `static/ui.js` by esbuild. After editing anything
under `static/src/`, run `make js-build` (one-time `npm install` first to
fetch esbuild). The bundle output is committed, so contributors who don't
touch JS never need Node — `node_modules/` is gitignored.

Project conventions: [`CLAUDE.md`](CLAUDE.md) (overrides apply
project-wide; honored by both human and AI contributors).

Pre-commit hooks: ruff (lint + format), mypy strict, pyupgrade
`--py314-plus`, detect-secrets, trailing-whitespace,
end-of-file-fixer, check-yaml/toml.

## Configuration

`.env` overrides (all `APP_` prefix, parsed by pydantic-settings):

| var | default | what |
|---|---|---|
| `APP_LOG_LEVEL` | `INFO` | structlog level |
| `APP_JSON_LOGS` | auto | force json/console; auto = TTY detect |
| `APP_DB_PATH` | `phototag.db` | SQLite file location |
| `APP_MODELS_DIR` | `data/models` | weights cache |
| `APP_DEVICE` | `auto` | `auto` / `cpu` / `cuda` |
| `APP_API_TOKEN` | (unset) | shared secret for the UI; empty disables auth |
| `APP_API_TOKEN_FILE` | (unset) | file path to a secret; re-read per request (hot rotation) |

## Specs

- [`00-overview.md`](specs/00-overview.md) — goal, non-goals, version split
- [`01-architecture.md`](specs/01-architecture.md) — components, data flow
- [`02-stack.md`](specs/02-stack.md) — tech choices, rationale
- [`03-data-model.md`](specs/03-data-model.md) — SQLite schema (v1–v8)
- [`04-pipeline-tagging.md`](specs/04-pipeline-tagging.md) — scan/tag/persist
- [`05-clustering.md`](specs/05-clustering.md) — UMAP + HDBSCAN + naming
- [`06-search.md`](specs/06-search.md) — semantic search
- [`07-reporting.md`](specs/07-reporting.md) — HTML report
- [`08-xmp-categories.md`](specs/08-xmp-categories.md) — XMP sidecars
  + categories (pending impl)
- [`09-cli.md`](specs/09-cli.md) — CLI surface
- [`10-project-structure.md`](specs/10-project-structure.md) — repo layout
- [`11-roadmap.md`](specs/11-roadmap.md) — milestones, status
- [`12-performance.md`](specs/12-performance.md) — perf targets
- [`13-risks.md`](specs/13-risks.md) — risks + mitigations
- [`14-testing.md`](specs/14-testing.md) — test strategy
- [`15-faces.md`](specs/15-faces.md) — face detection / recognition /
  clustering (full design)
- [`16-improvement-plan.md`](specs/16-improvement-plan.md) — forward
  backlog with status

## License

Apache-2.0. See `pyproject.toml`.
