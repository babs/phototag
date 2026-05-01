# 10 — Project structure

```
phototag/
├── pyproject.toml
├── README.md
├── GETTING_STARTED.md
├── CLAUDE.md
├── Makefile
├── .pre-commit-config.yaml
├── .secrets.baseline
├── phototag/
│   ├── __init__.py
│   ├── cli.py                # typer entry — every command (scan, embed,
│   │                         #   cluster, report, prune, doctor, backup,
│   │                         #   list, stats, export, query, info, rename,
│   │                         #   rename-bulk, exif-backfill, geo-tag, serve,
│   │                         #   faces detect/cluster/verify/refine-noise/
│   │                         #     auto-attach/name/unname/clear-noise-labels/
│                             #     corrections/corrections-compact/purge/stats)
│   ├── scanner.py            # filesystem walk (os.walk, no symlink follow) + xxhash
│   ├── pipeline.py           # batch orchestration (scan_and_tag, embed_all)
│   ├── store.py              # SQLite — migrations (v1–v8), thread-local conns
│   │                         #   + write lock, every query
│   ├── settings.py           # pydantic-settings; APP_* env vars
│   ├── config.py             # ClusterConfig, ClipConfig, RamConfig, image extensions
│   ├── logging.py            # structlog + TTY detection
│   ├── exif.py               # EXIF extraction (date, camera, GPS) + sanitization
│   ├── geo.py                # offline reverse-geocoding (cities-1000 via reverse_geocoder)
│   ├── clustering.py         # UMAP + HDBSCAN + TF-IDF naming (image clusters)
│   ├── reporting.py          # Jinja2 static HTML report
│   ├── faces.py              # FaceDetector, cluster_faces, cluster_orphan_faces,
│   │                         #   attach_face_to_best_identity, auto_attach_orphans,
│   │                         #   apply_sticky_corrections (tier-1 + tier-2 cannot-link),
│   │                         #   _hungarian_identity_match, verify_faces, name_cluster
│   ├── ui.py                 # FastAPI app — every HTTP endpoint, CORS, optional
│   │                         #   APP_API_TOKEN[_FILE] middleware, lifespan
│   └── models/
│       ├── __init__.py
│       ├── base.py           # Tagger / Embedder Protocols
│       ├── ram.py            # RAM++ wrapper (lazy-imported behind [ram] extra)
│       └── clip.py           # open_clip wrapper (lazy-imported behind [clip] extra)
├── static/                   # served at /static — single-file SPA, no bundler
│   ├── ui.js                 # vanilla JS — lightbox, face overlays, popover,
│   │                         #   sidebar filter, triage queue, fringe view,
│   │                         #   keyboard shortcuts, hash sync
│   └── ui.css
├── templates/
│   ├── ui.html               # SPA shell (renders /static/ui.js + window.PHOTOTAG_API_TOKEN)
│   ├── cluster.html.j2       # report per-cluster page
│   └── index.html.j2         # report index page
├── tests/                    # 83+ tests (pytest)
│   ├── conftest.py           # tmp_db fixture
│   ├── test_cli.py           # version, prune, list, stats, export, doctor, backup
│   ├── test_scanner.py       # iter_images, hash_file
│   ├── test_store.py         # migrations, image upsert, embeddings, delete cascade
│   ├── test_store_faces.py   # faces table + clusters + identities + sticky +
│   │                         #   attach_face_to_best_identity (margin / cannot-link /
│   │                         #   noise detach), auto_attach_orphans, edge gallery
│   ├── test_faces_verify.py  # heuristic verify pass
│   ├── test_exif.py          # EXIF extraction round-trip
│   └── test_ui_api.py        # FastAPI surface — every endpoint
├── scripts/
│   ├── progress.sh
│   └── refine_noise.py       # standalone image-cluster noise-refinement script
├── specs/                    # design docs (this file is one of them)
└── data/                     # gitignored; DBs, model weights, EXIF cache, thumbs,
                              #   preview JPEGs, face thumbnails, server logs, backups
```

Schema migrations live inline in `phototag/store.py:MIGRATIONS` (a list
of SQL strings; numbered v1–v8 in code comments). Each migration is
applied atomically inside its own `executescript`; the `meta(key,value)`
table records `schema_version`.

## Module boundaries

- `models/base.py` defines `Tagger` and `Embedder` Protocols. Concrete classes (RAM, CLIP, …) implement them. Pipeline takes them by interface, never by concrete type.
- `store.py` is the only module that touches SQLite. Every other module passes a `Store` instance.
- `cli.py` is thin — only argument parsing + delegating to other modules.

## pyproject.toml essentials

- Package name: `phototag`
- Entry point: `phototag = phototag.cli:app`
- Optional extras: `[gpu]`, `[heic]`, `[raw]`, `[vec]`, `[report]`. Lets users install minimal core, add what they need.
- Lockfile via `uv` or `pip-tools` — pin transitives.

## Conventions

- Python 3.14+ (matches `pyproject.toml` `requires-python` and `ruff target-version = "py314"`).
- Type hints everywhere; `mypy --strict` in CI.
- Format: `ruff format`. Lint: `ruff check`.
- No comments unless WHY is non-obvious (per global rules).
- `__init__.py` re-exports public API only.
