# CLAUDE.md — phototag

**READ BEFORE WRITING ANY CODE.** No exceptions. If you skip this, you will redo work.

## Mandatory pre-flight

1. Read `~/.claude/CLAUDE.md` — global rules. Includes `~/.claude/USER_GLOBAL_NOTES.md` and contains the rule "Read all rules before writing code — before generating any code, check all applicable rule files (`~/.claude/rules/*.md`) for the relevant language/tooling. User rules override spec defaults when they conflict."
2. Read every applicable file under `~/.claude/rules/` for the languages/tools you will touch (e.g. `python.md`, `bash.md`, `dockerfile.md`, `github-actions.md`, `gitlab-ci.md`). Don't cherry-pick.
3. Read the relevant spec under `specs/` for the feature being touched. The spec is the source of truth for design, schema, and CLI shape.
4. If initializing or aligning Python tooling, invoke the `python-init` skill (FastAPI-flavored — adapt for this CLI) instead of hand-rolling pyproject/pre-commit.

If you have already started writing code without doing 1–3, **stop and re-do them**. Patching after the fact produces a half-aligned project.

## Project conventions

- **Dependency manager**: `uv`. Never `pip install` directly. Lockfile is `uv.lock`.
- **Run scripts**: `uv run phototag ...` or `uv run pytest`.
- **Heavy ML deps are extras**: `[ram]`, `[clip]`, `[cluster]`, `[report]`, `[heic]`, `[raw]`, `[exif]`, `[ui]`. Core install must work without them.
- **Logging**: structlog with TTY detection (`phototag.logging.setup_logging`). Semantic events: `log.info("scan_completed", scanned=n, tagged=k)`.
- **Settings**: pydantic-settings, env prefix `APP_`, `.env` supported.
- **Type hints**: modern only (`list[str]`, `str | None`). Never `List`, `Optional`.
- **DB**: single SQLite file. WAL mode. Schema migrations numbered in `phototag/store.py`.
- **Read-only corpus**: `data/pictures/` is a symlink to the user's actual library. Never write inside it. Persist all derived state under `data/` or the DB. Image rows store paths *relative to the DB's parent directory* (e.g. `pictures/foo.jpg`); always go through `Store.absolute_path()` / `Store.relative_path()` instead of touching `images.path` directly.

## Commands

```sh
uv sync                    # core install
uv sync --extra all        # with all ML extras
uv run pre-commit install
uv run pytest              # fast tests only
uv run pytest -m slow      # tests requiring real models
make lint                  # pre-commit on all files
```

## When in doubt

Re-read this file. Re-read `specs/`. Then act.
