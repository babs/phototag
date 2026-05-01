# 09 — CLI

Single binary `phototag`, built with `typer`. Subcommands grouped by lifecycle stage.

## Global flags

| Flag | Default | Effect |
|---|---|---|
| `--db PATH` | `./phototag.db` | SQLite location |
| `--gpu / --cpu` | auto | Force device |
| `--batch-size N` | auto | Inference batch size |
| `--verbose` / `-v` | off | Per-image log lines |
| `--quiet` / `-q` | off | Suppress progress |

## v1 — discovery loop

```
phototag scan PATH [--threshold 0.68] [--force] [--force-tag]
phototag embed PATH
phototag cluster [--min-size 20] [--name-with ram,clip] [--min-samples 5]
phototag report  [--out report/] [--serve]
phototag info IMAGE_PATH
```

| Command | Purpose |
|---|---|
| `scan` | Walk path, hash, tag new/changed images |
| `embed` | Compute + cache CLIP embeddings |
| `cluster` | UMAP + HDBSCAN run, persist cluster_run + assignments |
| `report` | Generate static HTML report (`--serve` runs local FastAPI) |
| `info` | Inspect a single image's tags + metadata |

## v1.5 — polish & search

```
phototag query "TEXT" [--limit 30] [--embedder NAME]
phototag list  --tag NAME [--tag NAME] [--score-min 0.7] [--limit 100] [--format json|tsv]
phototag stats [--top 50] [--kind label|geo]
phototag export [--format json|csv] [--out FILE] [--min-score 0.0]
phototag prune [--apply] [--limit N]
phototag doctor [--fix]
phototag backup [--out PATH]
phototag rename CLUSTER_ID [LABEL]
phototag rename-bulk JSON_PATH
```

| Command | Purpose |
|---|---|
| `query` | Semantic search by text against cached CLIP embeddings |
| `list` | Filter images by tag(s) / score |
| `stats` | Tag distribution, top N, image + face counts |
| `export` | Dump tags/metadata to JSON or CSV |
| `prune` | Drop DB rows whose file is gone from disk (default dry-run) |
| `doctor` | Health-check the DB; flag size mismatches, orphan identities, schema-version drift; `--fix` recomputes safe items |
| `backup` | Create an SQLite snapshot of the DB (atomic, online; default dst `data/backups/phototag-<UTC-iso>.db`) |
| `rename` / `rename-bulk` | Bulk-set `clusters.label_user` |

## v2 — productivity

```
phototag xmp write PATH [--threshold 0.7] [--include-people] [--apply]
phototag xmp clean PATH [--apply]

phototag category add NAME
phototag category map --tag T   --category C
phototag category map --cluster N --category C
phototag category list
phototag category apply

phototag faces detect              [--limit N] [--force] [--i-understand]
phototag faces cluster             [--min-size 3] [--min-samples 2]
phototag faces verify              [--min-score 0.65] [--min-area 1024] [--apply]
phototag faces refine-noise        [--min-size 3] [--min-samples 2] [--persist]
phototag faces auto-attach         [--threshold 0.5] [--auto-verify-threshold 0.7]
                                   [--limit N] [--persist]
phototag faces name                CLUSTER_ID NAME
phototag faces unname              CLUSTER_ID
phototag faces clear-noise-labels
phototag faces corrections         [--action ACT] [--face-id N] [--limit N]
phototag faces corrections-compact [--apply]
phototag faces stats
phototag faces purge [--keep-identities] [--yes]
phototag faces stats
phototag faces report [--out report-faces/]

phototag exif-backfill [--limit N] [--force]
phototag geo-tag       [--limit N] [--force]
phototag serve         [--host 127.0.0.1] [--port 8000]
```

Faces are **opt-in** and biometric — see [`15-faces.md`](15-faces.md).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Generic error |
| 2 | Bad usage / arg |
| 10 | DB locked / migration mismatch |
| 11 | Model not found / download failed |
| 12 | OOM after retry |

## Output conventions

- `--format json` always available on read commands; default for piping.
- TTY: human-friendly tables (rich); non-TTY: machine-friendly TSV by default.
- All commands accept `--db` to point at alt DBs (testing, multiple libraries).
