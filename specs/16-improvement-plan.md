# 16 — Improvement plan

Living roadmap for the post-v2 face-management surface. Items are bucketed
by impact area; each carries a one-line problem statement, the proposed
mechanism, an effort estimate, and a status. **Status legend**: 🟢 shipped,
🟡 in flight, ⬜ pending. Effort in working hours of focused work.

## Matching quality

| # | Item | Status | Effort | Notes |
|---|---|---|---|---|
| 1 | Confidence badge in face overlay | 🟢 | 0.5 h | `attach_face_to_best_identity` returns `{name, sim, user_verified}`; `list_faces_for_image` surfaces `attach_sim` for manual-run assignments; JS shows `· 0.86` after the label |
| 2 | "Did you mean: …" top-K suggestions in popover | 🟢 | 1 h | `GET /api/faces/{id}/suggest?k=3` runs cosine vs identity centroids (vectorized); popover renders one-click chips that reuse the manual-name path (detach-from-noise + auto-validate) |
| 3 | Bulk auto-attach orphan faces | 🟢 | 1 h | `phototag faces auto-attach [--persist]` + `POST /api/faces/auto-attach-orphans?dry_run=`; vectorized cosine matmul |
| 4 | Per-identity threshold tuning | ⬜ | 4 h | track per-identity sim distribution; raise auto-validate threshold for high-variance identities (kids growing, etc.) |
| 5 | Hard-negative mining from `face_corrections` (tier-2 sticky) | 🟢 | 4 h | `unassigned` rows build a per-face cannot-link set on the rejected cluster's `label_user`; both `attach_face_to_best_identity` and `auto_attach_orphans` skip those identities so the system never re-suggests a name the user already rejected for that face |
| 6 | Identity merge / split UI | 🟢 | 1 h | `POST /api/face-identities/merge` (body `{survivor, loser}`); sample-weighted centroid blend (cap=200, mirrors `IDENTITY_SAMPLE_CAP`), summed `n_samples`, re-labels loser's clusters to survivor, drops the loser row. Person edit (✏️) shows a "merge into…" autocomplete (datalist sourced from `/api/people/names`). Split was already shipped. |
| 7 | Constrained HDBSCAN (tier-3 sticky) | ⬜ | 2 d | semi-supervised clusterer with must-link / cannot-link constraints from `face_corrections`; new dependency (`constrained-clustering` or hand-rolled) |

## Flow / UX

| # | Item | Status | Effort | Notes |
|---|---|---|---|---|
| 8 | "Next unidentified" key (N) | 🟢 | 0.5 h | jumps to next photo with ≥1 orphan face within current view; falls back to loading the orphan photo list |
| 9 | Validate-and-advance | 🟢 | 0.5 h | clicking V (validate) auto-advances popover to next un-verified named face on the same image; falls back to plain close+refresh when nothing is left |
| 10 | Photo triage queue | 🟢 | 1 h | `GET /api/faces/triage` (`store.list_triage_images`) returns `{id, path, n_unverified, n_dups, score}` with `score = n_unverified + 2*n_dups`; sidebar pins a "triage queue" entry next to noise/orphan; workspace grid seeds `viewIds` so ←/→ in the lightbox walks the queue |
| 11 | Identity gallery edge view | 🟢 | 1 h | on `/api/people/by-name/{name}` show 9 most-distant-from-centroid faces (the ambiguous edge) for quick triage |
| 12 | Re-cluster preview members | ⬜ | 4 h | dry-run output already has cluster IDs; UI expander showing the ~5 faces nearest each centroid before persisting |
| 13 | Drag-to-redraw bbox | ⬜ | 4 h | per-image bbox edit + re-embed via insightface; biggest UX upside but heaviest impl |

## Performance / correctness

| # | Item | Status | Effort | Notes |
|---|---|---|---|---|
| 14 | Vectorized cosine for bulk attach | 🟢 | 1 h | one `(N, D) @ (D, M)` matmul; ~50× faster than the per-face Python loop |
| 15 | Identity `n_samples` cap for blend | 🟢 | 0.5 h | `IDENTITY_SAMPLE_CAP=200` applied at every blend site (`cluster_faces`, `cluster_orphan_faces`, `attach_face_to_best_identity`, `merge_face_identities`); raw `n_samples` counter preserved for display |
| 16 | `cluster_assignments.distance` metric coherence | 🟢 | 4 h | v9 migration adds `face_cluster_assignments.distance_kind` ('euclidean_umap' \| 'cosine_dist'); `assign_face_to_cluster` records the kind, API surfaces it, fringe view labels it (`d=0.18 (umap)` / `d=0.14 (cos)`). Cluster-member ORDER BY left as-is on purpose — the visible kind tag is the fix, not a re-sort |
| 17 | `face_corrections` retention compactor | 🟢 | 0.5 h | `phototag faces corrections-compact [--apply]` collapses to one row per face_id (most-recent wins); mirrors the dedup the sticky pass does anyway |
| 18 | Sticky-pass scan budget | 🟢 | 0.5 h | SQL filter to `action IN ('named','unassigned')`; per-face dedup tightened so `verified` rows can't mask older `named` |

## Operational

| # | Item | Status | Effort | Notes |
|---|---|---|---|---|
| 19 | `phototag doctor` health check | 🟢 | 4 h | walks DB, flags `face_clusters.size` / `clusters.size` mismatches, faces without embedding, orphan identities, schema_version drift; `--fix` recomputes the safe items |
| 20 | `phototag backup` SQLite snapshot | 🟢 | 0.25 h | `phototag backup [--out PATH]` uses `sqlite3.Connection.backup` on a fresh source connection; atomic via `<dst>.tmp` → rename; default dst `data/backups/phototag-<UTC-iso>.db` |
| 21 | Token rotation without restart | 🟢 | 0.25 h | `APP_API_TOKEN_FILE=` watched per request; lets the user rotate by editing the file |
| 22 | XMP sidecar writer (v2 leftover) | 🟢 | 4 h | `phototag xmp write/clean` via `exiftool` subprocess (`phototag/xmp.py`); idempotent (mtime + subject-set check); atomic tmp→rename; `--include-people` adds validated face labels; system dep documented in README + pyproject `[xmp]` extra |
| 23 | Categories + tag/cluster mapping (v2 leftover) | ⬜ | 1 d | schema + CLI + UI mapping rules (see `08-xmp-categories.md`) |
| 24 | CI pipeline | ⬜ | 4 h | GitHub Actions workflow: ruff + mypy + pytest -m "not slow"; nightly slow run |
| 25 | JS bundling / module split | 🟢 | 1 d | source moved to `static/src/{state,api,lightbox,sidebar,workspace,keyboard,runs,main}.js`; esbuild bundles to `static/ui.js` (single ES2020 IIFE) via `make js-build` / `make js-watch`; `package.json` + `package-lock.json` committed, `node_modules/` gitignored; bundle output stays committed so the app works without running the bundler; `<script>` cache-buster `?v={{ version }}` unchanged. Fallback path when no bundler is installed: `make js-build` prints an install hint and exits non-zero — contributors run `npm install` once. |
| 26 | Photo paths relative to the DB | 🟢 | 0.5 d | `images.path` now stores paths *relative to the DB's parent directory* (so `data/full.db` + `data/pictures/foo.jpg` → row `pictures/foo.jpg`); no extra `meta` key — anchor is implicit (`db_path.parent`). `Store.absolute_path(stored)` / `Store.relative_path(abs)` are the only entry points, and every reader (prune, redetect-faces, EXIF backfill, thumb/preview/raw/face-thumb endpoints, faces detect/embed pipelines, reporting) goes through them. Absolute strings still resolve unchanged for legacy rows / `/tmp` test paths. Symlink renamed `data/photo-corpus/` → `data/pictures/`. Pre-release migration was a one-shot SQL `UPDATE` (no schema bump). |

## What's already shipped (current state)

Hardening:
- thread-local SQLite + write lock + busy_timeout
- atomic JPEG cache writes (incl. /face-thumb), PIL handle leaks fixed
- transactional schema migrations through v9
  (v5 verified, v6 corrections, v7 user_verified, v8 tag.kind, v9 distance_kind)
- DOM-built event listeners (no inline onclick interpolation → no XSS surface)
- `phototag prune` for stale-row cleanup
- `phototag doctor` for DB health + auto-fix (parallel SELECTs;
  `ok` flag respects post-fix state)
- `phototag backup` atomic SQLite snapshot
- optional `APP_API_TOKEN` / `APP_API_TOKEN_FILE` middleware
  (constant-time compare; CORS preflight passes through; file-watched rotation)

Faces:
- detect / cluster / verify / refine-noise / auto-attach / clear-noise-labels
- per-face validate, drop-dups, drop-unidentified, redetect (preserves
  validated faces by IoU)
- Hungarian identity assignment + sample-weighted centroid blend
  (`IDENTITY_SAMPLE_CAP=200`)
- tier-1 sticky-label correction replay on every cluster pass
- tier-2 hard-negative mining (cannot-link from `unassigned` corrections)
- identity merge UI + endpoint (sample-weighted blend; audit-logged)
- corrections-compact CLI (preserves every `unassigned` row so cannot-link
  survives the compaction)
- distance_kind column on `face_cluster_assignments` (Euclidean vs cosine)

CLI:
- list / stats / export / query (CLIP semantic search)
- prune / doctor / backup / rename / rename-bulk
- faces detect / cluster / verify / refine-noise / auto-attach / name /
  unname / clear-noise-labels / corrections / corrections-compact / stats / purge
- xmp write / clean (sidecar export via exiftool subprocess)

UI:
- single-file SPA → ESM modules (`static/src/*.js` → esbuild bundle)
- noise/orphan sidebar selector + triage queue selector
- quick filter (AND tokens, bold matches, X clear)
- top-tags collapsible
- ⚠ duplicate-name hint, `?` un-validated marker, sim badge for
  auto-attached, ✓ implicit on validated, distance_kind tag in fringe view
- top-K identity suggestions on the popover (cannot-link aware)
- person merged view + edge gallery (9 most-uncertain) + identity merge
- "?" keyboard help overlay
- `N` — jump to next unidentified, `V` — validate-and-advance,
  `G` — go-to-person, `W` — wrong, `X` — drop dups

Specs aligned: 02 (Python 3.14), 03 (v5/v6/v7/v9 schema), 09 (full CLI),
11 (roadmap status), 12 (memory budget), 13 (UI exposure), 15 (face API
+ recluster + auto-attach + IoU semantics).

## Picking the next sprint

Six items remain (⬜ above) — listed by daily-flow ROI:

**Matching quality** (1 day each):
- **#4** — per-identity threshold tuning. Use the per-attach sim
  distributions we collect to raise `auto_verify_threshold` for
  high-variance identities (kids growing, glasses on/off).
- **#12** — re-cluster preview members. The dry-run already returns the
  cluster IDs; surface the ~5 faces nearest each centroid in a UI
  expander before persisting.

**v2 leftover** (1 day):
- **#23** — categories + tag/cluster mapping (see `08-xmp-categories.md`).
  Pairs with the now-shipped #22 XMP writer to populate the `lr:HierarchicalSubject`
  field.

**Portability** — *shipped*:
- **#26** — photo paths now stored relative to the DB's parent directory
  (no `meta.photo_root` needed; anchor is implicit). `data/photo-corpus`
  symlink renamed to `data/pictures`. `Store.absolute_path()` /
  `Store.relative_path()` are the canonical accessors; absolute strings
  still resolve unchanged for legacy rows and tests using `/tmp`.

**Bigger swing** (~2 days each):
- **#7** — constrained HDBSCAN (tier-3 sticky). Semi-supervised cluster
  pass that consumes both `named` and `unassigned` corrections as
  must-link / cannot-link constraints during clustering itself (today
  cannot-link is enforced post-cluster at the identity-match step).
  Best-quality clustering improvement; likely new dependency.
- **#13** — drag-to-redraw bbox (deferred). Per-image bbox edit + re-embed
  via insightface. Largest UX upside but heaviest impl. Skipped this
  sprint pending a clearer design discussion.

**Infra** (independent track, deferred):
- **#24** — CI pipeline. GitHub Actions: ruff + mypy + pytest -m "not
  slow"; nightly slow run. Long-shot at the moment.

Recommended next pick: **#26** (photo paths relative) before any user
ships a real corpus — it's a schema change that cascades everywhere file
paths are read, easier to do while no real user data depends on the
current shape. Then **#23** to round out the v2 surface.
