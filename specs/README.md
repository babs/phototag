# Specs — phototag

Local, scriptable photo-tagging tool. Scans a library, tags via RAM++, derives a taxonomy a posteriori via CLIP embeddings + UMAP + HDBSCAN.

## Read order

| # | File | Topic |
|---|------|-------|
| 00 | [overview.md](00-overview.md) | Goal, scope, version split |
| 01 | [architecture.md](01-architecture.md) | Components, data flow |
| 02 | [stack.md](02-stack.md) | Tech choices, rationale |
| 03 | [data-model.md](03-data-model.md) | SQLite schema |
| 04 | [pipeline-tagging.md](04-pipeline-tagging.md) | v1 scan/hash/tag/persist |
| 05 | [clustering.md](05-clustering.md) | v1 UMAP + HDBSCAN + naming |
| 06 | [search.md](06-search.md) | v1.5 semantic search |
| 07 | [reporting.md](07-reporting.md) | v1 HTML report |
| 08 | [xmp-categories.md](08-xmp-categories.md) | v2 XMP sidecar, category mapping |
| 09 | [cli.md](09-cli.md) | CLI surface |
| 10 | [project-structure.md](10-project-structure.md) | Repo layout |
| 11 | [roadmap.md](11-roadmap.md) | Milestones, effort |
| 12 | [performance.md](12-performance.md) | Perf targets |
| 13 | [risks.md](13-risks.md) | Risks, mitigations |
| 14 | [testing.md](14-testing.md) | Test strategy |
| 15 | [faces.md](15-faces.md) | v2 face detection / recognition / clustering (opt-in) |

## Source

Derived from `../plan2.md` (original French plan).
