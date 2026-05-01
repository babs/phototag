# 10 — Project structure

```
phototag/
├── pyproject.toml
├── README.md
├── phototag/
│   ├── __init__.py
│   ├── cli.py                # typer entry
│   ├── scanner.py            # filesystem walk + hash
│   ├── pipeline.py           # batch orchestration
│   ├── store.py              # SQLite (sqlmodel or raw sqlite3)
│   ├── exif.py
│   ├── config.py
│   ├── models/
│   │   ├── __init__.py
│   │   ├── base.py           # Tagger / Embedder protocols
│   │   ├── ram.py            # RAM++ wrapper
│   │   └── clip.py           # open_clip wrapper          (v1)
│   ├── clustering.py         # UMAP + HDBSCAN + naming    (v1)
│   ├── search.py             # semantic search            (v1.5)
│   ├── reporting.py          # jinja2 HTML reports        (v1)
│   ├── xmp.py                # sidecar writer             (v2)
│   ├── faces.py              # detect + embed + cluster   (v2, opt-in)
│   └── migrations/
│       ├── 0001_init.sql
│       ├── 0002_embeddings.sql
│       └── 0003_categories.sql
├── templates/                # report templates           (v1)
│   ├── index.html.j2
│   └── cluster.html.j2
├── tests/
│   ├── conftest.py
│   ├── fixtures/             # 20–30 varied photos
│   ├── test_scanner.py
│   ├── test_store.py
│   ├── test_pipeline.py
│   ├── test_clustering.py
│   ├── test_search.py
│   └── test_xmp.py
└── data/
    └── models/               # downloaded RAM/CLIP weights (gitignored)
```

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

- Python 3.11+ only.
- Type hints everywhere; `mypy --strict` in CI.
- Format: `ruff format`. Lint: `ruff check`.
- No comments unless WHY is non-obvious (per global rules).
- `__init__.py` re-exports public API only.
