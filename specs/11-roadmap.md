# 11 — Roadmap

Effort in working days, single developer.

**Driver**: corpus content is unknown. Clustering + visual report is the fastest path to *seeing what's in there* — much more valuable than tag filtering on an unfamiliar library. Roadmap front-loads it.

## v1 — Discovery loop (~7 days)

Goal: from raw folder to navigable HTML report of clusters with thumbnails. End-to-end "what's in my library" answered.

| # | Task | Effort | Verifiable output |
|---|---|---|---|
| 1 | Project setup + minimal CI | 0.5 d | `pip install -e .` works |
| 2 | Scanner + hash + SQLite | 1 d | `scan` populates DB without tagging |
| 3 | RAM++ wrapper (`Tagger` interface) | 1 d | Tag a single image via CLI |
| 4 | Batch pipeline + GPU/CPU switch | 1 d | 1000 photos tagged without crash |
| 5 | CLIP wrapper + `embeddings` table | 1 d | `embed` populates vectors |
| 6 | Clustering (UMAP + HDBSCAN) | 1 d | Clusters persisted to DB |
| 7 | TF-IDF naming + CLIP zero-shot | 0.5 d | Auto-labels per cluster |
| 8 | HTML report with thumbnails | 1 d | `report` produces navigable file |
| — | **v1 shipped — corpus visible** | — | — |

**Gate** — Run on a 500–1000 photo sample. Inspect the report. Decide what to invest in next: more accuracy (better tagger, manual cluster validation) or more productivity (XMP, categories).

## v1.5 — Polish & search (~2.5 days)

Defer-until-needed extras that depend on v1 already exposing the corpus.

| # | Task | Effort | Verifiable output |
|---|---|---|---|
| 9 | Semantic search (`query`) | 0.5 d | `query "x-ray"` returns relevant images |
| 10 | `list` / `stats` / `export` | 0.5 d | CLI filtering by tag / cluster |
| 11 | EXIF extraction | 0.5 d | Date / GPS persisted, filterable |
| 12 | Cluster rename workflow | 0.5 d | `label_user` editable from report or CLI |
| 13 | Test suite hardening | 0.5 d | CI green on slow integration |

## v2 — Productivity (~3 days)

| # | Task | Effort | Verifiable output |
|---|---|---|---|
| 14 | XMP writer | 0.5 d | Tags visible in digiKam |
| 15 | Categories + tag/cluster mapping | 1 d | Configurable rules, persisted |
| 16 | Faces — detect + embed (`[face]` extra) | 0.5 d | `phototag faces detect` populates faces table |
| 17 | Faces — cluster + identity carry-over | 0.5 d | `phototag faces cluster` produces stable people |
| 18 | Faces — UI panel + lightbox overlay | 0.5 d | Click face → person gallery; rename propagates |

Faces details in [`15-faces.md`](15-faces.md). **Opt-in** via `--i-understand` on first run; processes biometric data, never leaves the machine.

## Total

~11 days end-to-end. v1 (~7 d) delivers the "see what's in my library" outcome; v1.5 and v2 are optional follow-ups.

## Critical path

```
1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → v1 (corpus visible)
                                ↓
                                9, 10, 11, 12, 13 → v1.5 (parallelizable)
                                ↓
                                14, 15 → v2
```

Tasks 9–13 are all independent and can run in any order. 14 and 15 are independent.
