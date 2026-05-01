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
| 16 | `cluster_assignments.distance` metric coherence | ⬜ | 4 h | mixes Euclidean (UMAP) and cosine (manual). Either normalize to z-score within cluster or add a `distance_kind` column |
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
| 25 | JS bundling / module split | ⬜ | 1 d | `static/ui.js` is at 1500+ lines; esbuild + module split keeps maintainability sane |

## What's already shipped (current state)

Hardening:
- thread-local SQLite + write lock + busy_timeout
- atomic JPEG cache writes, PIL handle leaks fixed
- transactional schema migrations (v8 = `tags.kind`, v7 = `faces.user_verified`)
- DOM-built event listeners (no inline onclick interpolation → no XSS surface)
- `phototag prune` for stale-row cleanup
- `phototag doctor` for DB health + auto-fix
- optional `APP_API_TOKEN` shared-secret middleware (constant-time compare;
  CORS preflight passes through)

Faces:
- detect / cluster / verify / refine-noise / auto-attach / clear-noise-labels
- per-face validate, drop-dups, drop-unidentified, redetect (preserves
  validated faces by IoU)
- Hungarian identity assignment + sample-weighted centroid blend
- tier-1 sticky-label correction replay on every cluster pass

CLI:
- list / stats / export / query (CLIP semantic search)
- prune / doctor / rename / rename-bulk
- faces stats reports unidentified + user_verified counts
- faces corrections audit dump

UI:
- noise/orphan sidebar selector
- quick filter (AND tokens, bold matches, X clear)
- top-tags collapsible
- ⚠ duplicate-name hint, ? un-validated marker, sim badge for auto-attached
- "?" keyboard help overlay
- N — jump to next unidentified

Specs aligned: 02 (Python 3.14), 03 (v5/v6/v7 schema), 09 (full CLI),
11 (roadmap status), 12 (memory budget), 13 (UI exposure), 15 (face API
+ recluster + auto-attach + IoU semantics).

## Picking the next sprint

Eight items remain (⬜ above). Grouped by ROI on the daily flow:

**Quick polish** (~half a day each):
- **#16** — distance-metric coherence. UI sorts cluster members by
  `face_cluster_assignments.distance` but the column mixes Euclidean
  (UMAP) and cosine-derived (manual). Either normalize per-cluster or
  add a `distance_kind` column. Cosmetic until you start sorting by it.

**Matching quality** (1 day each):
- **#4** — per-identity threshold tuning. Use the per-attach sim
  distributions we now collect to raise `auto_verify_threshold` for
  high-variance identities (kids growing, glasses on/off).
- **#12** — re-cluster preview members. The dry-run already returns the
  cluster IDs; surface the ~5 faces nearest each centroid in a UI
  expander before persisting.

**v2 leftovers** (1 day each):
- **#22** — XMP sidecar writer. `phototag xmp write/clean` via exiftool;
  makes tags portable to digiKam / Lightroom.
- **#23** — categories + tag/cluster mapping (see `08-xmp-categories.md`).
  Pairs with #22.

**Bigger swing** (~2 days each):
- **#7** — constrained HDBSCAN (tier-3 sticky). Semi-supervised cluster
  pass that consumes both `named` and `unassigned` corrections as
  must-link / cannot-link constraints. Best-quality clustering
  improvement; new dependency.
- **#13** — drag-to-redraw bbox. Per-image bbox edit + re-embed via
  insightface. Largest UX upside but heaviest impl.

**Infra** (independent track):
- **#24** — CI pipeline. GitHub Actions: ruff + mypy + pytest -m "not
  slow"; nightly slow run.
- **#25** — JS bundling / module split. `static/ui.js` is past 1500
  lines; esbuild + module split before the next big UI feature.

Recommended next pick if you want one half-day item: **#16** (cleans up
a long-standing inconsistency that will start mattering as the UI grows
sort/filter by distance). For a focused day: **#22** XMP writer (the
last v2 leftover that affects everyday use).
