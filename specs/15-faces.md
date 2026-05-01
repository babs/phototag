# 15 — Face detection, recognition, and clustering (v2)

Faces become a parallel track to RAM tags + CLIP clusters: detect → embed → cluster → name → browse. Same architectural shape as `04-pipeline-tagging.md` and `05-clustering.md`. **Opt-in, separate command, biometric-data aware** (see `13-risks.md`).

## Goal

For every photo in the library, find faces and group them by identity so the user can:
- See "all photos of person X" without having labelled anyone.
- Click a face on a photo and pivot to that person's gallery.
- Carry forward identities across re-scans.

## Stack

| Concern | Choice | Why |
|---|---|---|
| Detection | RetinaFace (via `insightface`) | Robust on phone shots, side faces, kids, masks |
| Embedding | ArcFace / iResNet100 (`insightface`) | 512-dim, state-of-the-art clustering quality |
| Runtime | `onnxruntime` (CPU) or `onnxruntime-gpu` | Same model, same weights, GPU optional |
| Clustering | UMAP + HDBSCAN | Re-use existing module |
| License | InsightFace under MIT-ish terms | Compatible with project Apache-2.0 |

`[face]` extra: `insightface>=0.7`, `onnxruntime>=1.18`. Weights (~200 MB) auto-download on first use into `data/models/insightface/`.

## Schema additions

```sql
-- v4: faces (one row per detected face)
CREATE TABLE faces (
    id           INTEGER PRIMARY KEY,
    image_id     INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    bbox_json    TEXT NOT NULL,            -- [x, y, w, h] in DETECT_MAX_SIDE coords
    det_score    REAL NOT NULL,            -- detector confidence
    embedding    BLOB NOT NULL,            -- float32 packed (512-dim ArcFace)
    dim          INTEGER NOT NULL,
    model_name   TEXT NOT NULL,            -- e.g. "insightface_buffalo_l_v1"
    landmarks_json TEXT                    -- 5 keypoints [[x,y]*5], optional
);
-- v5
ALTER TABLE faces ADD COLUMN verified INTEGER;
-- v6: audit trail of every user-driven face correction. No FK on
-- face_id/image_id so the row survives even when the underlying face
-- is deleted (the audit trail is the whole point).
CREATE TABLE face_corrections (
    id          INTEGER PRIMARY KEY,
    face_id     INTEGER,
    image_id    INTEGER,
    action      TEXT NOT NULL,             -- 'named' | 'unassigned' | 'deleted'
    cluster_id  INTEGER,                   -- old cluster on 'unassigned' / 'named'
    name        TEXT,                      -- new label on 'named'
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_face_corrections_face   ON face_corrections(face_id);
CREATE INDEX idx_face_corrections_action ON face_corrections(action);
CREATE INDEX IF NOT EXISTS idx_faces_image ON faces(image_id);

CREATE TABLE face_runs (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    params_json TEXT NOT NULL
);

CREATE TABLE face_clusters (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER NOT NULL REFERENCES face_runs(id) ON DELETE CASCADE,
    cluster_no INTEGER NOT NULL,           -- -1 = noise / unknown
    size       INTEGER NOT NULL,
    label_user TEXT,                       -- user-assigned name; persists across runs
    label_auto TEXT                        -- placeholder (e.g. "person 17")
);

CREATE TABLE face_cluster_assignments (
    face_id       INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
    cluster_id    INTEGER NOT NULL REFERENCES face_clusters(id) ON DELETE CASCADE,
    distance      REAL NOT NULL,
    distance_kind TEXT,                     -- v9: 'euclidean_umap' | 'cosine_dist'
    PRIMARY KEY (face_id, cluster_id)
);
```

`label_user` is the load-bearing field. When the user names a cluster ("Anne"), the name persists. Re-clustering produces new cluster IDs but a stable `label_user → identity_id` map (kept in `face_identities`) lets us reattach names on the next run via Hungarian matching on centroid similarity.

```sql
CREATE TABLE face_identities (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE,
    centroid  BLOB NOT NULL,               -- canonical centroid for re-association
    dim       INTEGER NOT NULL,
    n_samples INTEGER NOT NULL
);
```

## Pipeline

### Detect + embed

```
phototag faces detect [--i-understand] [--limit N]
```

For each image without a face row:
1. Decode → EXIF-transposed → BGR → resize to `DETECT_MAX_SIDE` (1280 px max side). Bboxes are stored in this coord space — same as the lightbox preview, so the overlay JS can use them directly without rescaling.
2. RetinaFace pass: list of `(bbox, det_score, landmarks)`.
3. For each face: align via 5-point landmarks, ArcFace embedding (L2-normalized).
4. Persist all faces in one transaction per image.

Idempotent: skip if `faces` table already has rows for the image and the model name matches.

Decode is reusable from `pipeline._open_image`; the existing prefetch queue applies.

### Verify (heuristic)

```
phototag faces verify [--min-score 0.65] [--min-area 1024] [--apply]
```

Walks every face row and applies two cheap filters: `det_score < min_score`
and bbox area `< min_area` (in the DETECT_MAX_SIDE coord space). Without
`--apply` it just sets `faces.verified` to 1 (passed) or 0 (suspect),
which the UI renders as a dashed red border on the overlay. With
`--apply` the failing rows are deleted (cluster sizes auto-adjusted).
On the live corpus this trimmed ~33 % of detections (980 / 3013) — most
of them tiny side-of-frame fragments and low-confidence cars-mistaken-
for-faces.

### Cluster

```
phototag faces cluster [--min-size 3] [--min-samples 2]
```

Identical to `clustering.cluster` but on `faces.embedding`. `min_cluster_size` is lower because most people will appear in a handful of photos. After fitting:
- Compute centroid per cluster (excluding noise).
- Match centroids against `face_identities` via cosine similarity ≥ 0.5; carry `label_user` forward.
- Insert/update `face_identities` for unmatched named clusters.

### Name

```
phototag faces name CLUSTER_ID "Anne"
phototag faces unname CLUSTER_ID
```

Or, in the UI, type a name in the cluster page. Sets `face_clusters.label_user` and updates `face_identities`.

## API additions

```
GET    /api/people [?only_named=|only_unnamed=]   → cluster-level rows
GET    /api/people/names                          → name-level rows (deduped)
GET    /api/people/by-name/{name}                 → merged person view (every
                                                    cluster sharing the name +
                                                    aggregate counts)
GET    /api/people/by-name/{name}/clusters        → just the cluster rows
GET    /api/people/by-name/{name}/edge?limit=9     → N farthest-from-centroid
                                                    faces of this person
                                                    (DESC by distance), for
                                                    quick "ambiguous fringe"
                                                    triage. Each entry carries
                                                    `distance_kind` (v9:
                                                    'euclidean_umap' | 'cosine_dist')
                                                    so the UI can label the
                                                    scale.
POST   /api/people/by-name/{name}/rename          → rename every cluster of name
                                                    (noise cluster is skipped)
POST   /api/people/by-name/{name}/split           → suffix into "name 1", "name 2", …
POST   /api/face-identities/merge                 → body {survivor, loser}: blend
                                                    centroids by sample count
                                                    (cap=200), sum n_samples,
                                                    re-label every cluster of
                                                    loser → survivor, drop the
                                                    loser identity row.

GET    /api/people/{cluster_id}                   → cluster detail + members
POST   /api/people/{cluster_id}/name              → set/clear label_user
                                                    (refused on noise cluster)
POST   /api/faces/{face_id}/name                  → manual cluster, when no
                                                    `phototag faces cluster` run.
                                                    Auto-detaches the face from
                                                    any noise cluster and marks
                                                    user_verified=1.

POST   /api/faces/{face_id}/verify                → user_verified=1 + audit log
POST   /api/faces/{face_id}/unverify              → user_verified=NULL + audit
GET    /api/faces/{face_id}/suggest?k=3           → top-K identity suggestions
                                                    by cosine vs face_identities
                                                    centroids. No threshold —
                                                    caller decides. Returns
                                                    [{name, sim, n_samples}, …]
                                                    sorted by sim desc.

GET    /api/images/{id}/faces                     → faces on this image
                                                    (now includes user_verified)
DELETE /api/images/{id}/faces                     → drop all faces (e.g. crowd)
DELETE /api/images/{id}/faces/unidentified        → drop only un-named faces
DELETE /api/images/{id}/faces/dups-of/{label}     → drop other un-validated
       ?keep_face_id={id}                          faces with this name on this
                                                    image (validated dups are
                                                    spared)
POST   /api/images/{id}/faces/validate-named      → bulk validate every named-
                                                    but-not-yet-validated face
POST   /api/images/{id}/redetect-faces            → re-run RetinaFace+ArcFace.
                                                    Validated faces whose box
                                                    overlaps a new detection
                                                    (IoU ≥ 0.4) are preserved
                                                    with their existing
                                                    embedding; un-matched
                                                    validated faces are dropped.

DELETE /api/faces/{id}                            → false-positive removal
POST   /api/faces/{id}/unassign                   → "wrong cluster" — drops the
                                                    face's cluster row in the
                                                    most recent run holding it
                                                    (so noise-only members
                                                    become true orphans)
GET    /api/faces/corrections                     → audit log: named /
                                                    unassigned / deleted /
                                                    verified / unverified

GET    /api/faces/unidentified/summary            → count of orphan/noise faces
GET    /api/faces/unidentified/images             → photos containing ≥1 of them
GET    /api/faces/triage?limit=300                → photos needing attention:
                                                    ≥1 unverified named face
                                                    OR a duplicate name (same
                                                    label_user on ≥2 faces of
                                                    the photo). Each row:
                                                    {id, path, n_unverified,
                                                    n_dups, score} with
                                                    score = n_unverified
                                                          + 2 * n_dups,
                                                    sorted by score DESC.
DELETE /api/faces/unidentified?yes=true           → library-wide drop
                                                    (yes=true required)
POST   /api/faces/clear-noise-labels              → wipe label_user from any
                                                    noise cluster (recovery
                                                    for the historic bug where
                                                    naming noise mass-tagged
                                                    its members)
POST   /api/faces/recluster-orphan                → re-run UMAP+HDBSCAN on the
       ?dry_run=true|false                          orphan/noise pool only.
       &min_size=&min_samples=                      dry_run=true returns the
                                                    proposed clustering and any
                                                    identity matches without
                                                    writing; dry_run=false
                                                    persists a new face_run
                                                    and detaches the orphan
                                                    faces from prior clusters.
                                                    Named clusters never
                                                    touched.
POST   /api/faces/auto-attach-orphans              → vectorized cosine match of
       ?dry_run=true|false                          every orphan face against
       &threshold=0.5                               face_identities centroids.
       &auto_verify_threshold=0.7                   Matches ≥ threshold join the
       &limit=N                                     identity's manual cluster;
                                                    sim ≥ auto_verify_threshold
                                                    also flips user_verified=1.
                                                    Returns per-identity
                                                    histogram + counts.

GET    /face-thumb/{face_id}                      → cropped face JPEG (cached)
```

### Corrections audit (`face_corrections`)

Every user action is logged to `face_corrections(face_id, image_id, action,
cluster_id, name, created_at)`. The next `phototag faces cluster` pass can
read it to seed soft constraints:

| logged action | proposed constraint at re-cluster |
|---|---|
| `named` (face X assigned to "Anne") | must-link X with Anne's identity centroid |
| `unassigned` (face X removed from cluster Y) | cannot-link X with the centroid that becomes Y's successor |
| `deleted` | not used (row is gone) |

Constraints are applied at three tiers, all shipped. Plan, in
order of cost:

1. **Sticky-label post-pass** (cheap; ~1 day) — *shipped*. After
   `cluster_faces` produces new clusters, walk `face_corrections` and apply:
   - `named`: force `face_clusters.label_user = name` for any cluster
     containing this face; update the identity centroid with this face.
   - `unassigned`: if this face landed in a cluster whose identity matches
     the old (wrong) cluster's identity, reassign to noise (`cluster_no = -1`).
   This means user actions persist across re-clustering with no algorithm
   change. Lossy on edge cases but predictable.
2. **Hard-negative mining (tier-2 sticky labels)** — *shipped*. The
   sticky pass parks the face in noise but the next attach pass can still
   re-suggest the same wrong identity via cosine. Tier-2 closes that loop:
   for every `unassigned` correction we look up the rejected cluster's
   `label_user` and build a per-face *cannot-link* set. Both
   `attach_face_to_best_identity` (per-face SQL via
   `cannot_link_identities_for_face`) and `auto_attach_orphans` (bulk SQL
   via `cannot_link_identities_for_faces`, materialized as a
   `(N_orphans, N_idents)` boolean mask zeroed to `-inf`) skip those
   identities, so the same wrong name can never win the argmax or count
   toward the top-2 ambiguity check. The bulk path reports
   `cannot_link_skipped` (count of forbidden (face, identity) pairs).
3. **Constrained HDBSCAN (tier-3 sticky)** — *shipped, opt-in*.
   `cluster_faces(..., tier3_constraints=True)` (CLI:
   `phototag faces cluster --tier3-constraints`). Hand-rolled, no new
   dependency: precompute a Euclidean distance matrix on the UMAP-
   reduced space, then surgically rewrite must-link pairs (anchor-spoke
   per `named` name, O(n) edges) → 0.0 and cannot-link pairs
   (`unassigned` against a labelled cluster vs every other face named
   that label, capped at 50k edges) → 99.0. HDBSCAN runs with
   `metric='precomputed'`. **Caveat**: distance-matrix surgery is not a
   hard guarantee — HDBSCAN's mutual-reachability is transitive, so two
   cannot-link points can still end up in the same cluster via a third
   bridging point. The post-pass tier-1 (replay named) + tier-2
   (cannot-link inside the attach helper) still run on top — tier-3 is
   a *during-clustering* nudge that lifts average quality, the
   post-passes provide the strict enforcement.

The corrections table is the substrate for all three tiers — every
action already logged today is consumed by the appropriate tier.

## UI

### People panel

A new sidebar tab next to "clusters" called **people** lists face clusters by size (or alphabetically when named). Click → person page: face-crop thumbnails on top, full photos containing that person below.

### Lightbox face overlay

When a single image is open in the lightbox:
- Fetch `/api/images/{id}/faces`.
- For each face, render an absolutely-positioned `<div>` overlay matching the bbox, scaled to the displayed image size.
- Reliability: layout/load races mean `clientWidth` can be 0 or `load` may not fire on cached images. The render is driven by a single `ResizeObserver` on the `<img>` plus a `decode()` race plus a `requestAnimationFrame` fallback; whichever resolves first triggers the render, all gated by a `lightboxToken` so stale paints from the previous image are dropped during fast navigation. The observer reads the *live* token (not a captured stale one — that was a regression).
- Style: 2 px border in a stable per-cluster colour (hash cluster_id → hue), name floating below the box. **Suspect** detections (verified=0 or det_score < 0.65) get a dashed red border instead.
- Clicking any face opens a two-row popover:
  - **row 1**: name input + **save** (Enter)
  - **row 2**: **view (V)**, **wrong (W)**, **delete (D)**, **close (Esc)**. View/Wrong are conditionally hidden when not applicable.
  - Keyboard shortcuts: V / W / D fire the matching button when the input isn't focused; Esc closes; Enter saves. The lightbox's own ←/→/F/T are suppressed while the form is open.
  - **save** behaviour: if the face is in a cluster, renames *that* cluster (label_user); if not, creates a "manual" face_run + cluster keyed by the new name (so identity propagates on the next `phototag faces cluster`).
- Lightbox info bar holds the secondary toggles and aggregate actions:
  - `faces (F)` — toggle overlay visibility (default on)
  - `drop N faces` — one-click for crowd shots
  - `tags (T) · N` — toggle the chip cloud (default off; user expands when they want to filter from the photo)
  - `re-detect faces` — re-runs RetinaFace+ArcFace on just this image
- The image bar / lightbox itself uses ←/→/PageUp/PageDown/j/k/Space for navigation; F and T for the overlays; the ✕ at top-right or click-on-backdrop for close.

### Visual

```
┌────────────────────────────────┐
│   ╔══╗      ╔══╗               │   ← overlays (one per face)
│   ║Anne║    ║?║                 │
│   ╚══╝      ╚══╝               │
│                                 │
│   [photo content]               │
└────────────────────────────────┘
```

## Privacy & ethics

This feature processes biometric data. Hard rules:

1. **Opt-in only.** No part of `phototag scan` triggers detection.
2. **First-run prompt.** `phototag faces detect` requires `--i-understand` on the first run; persists a marker in `meta` table so subsequent runs don't re-prompt.
3. **Local only.** Embeddings never leave the machine; no telemetry.
4. **Disclosure file.** First run writes `data/FACES_README.md` documenting what's stored, why, and how to wipe it (`phototag faces purge`).
5. **Wipe is total.** `phototag faces purge` drops every faces-related row including `face_corrections`. `--keep-identities` keeps `face_identities` and the corrections audit trail but drops embeddings, clusters, runs, and assignments.
6. **Don't process other people's libraries** without their consent. The README states this explicitly.

## CLI summary

```
phototag faces detect          [--limit N] [--force] [--i-understand]
phototag faces cluster         [--min-size 3] [--min-samples 2]
                               [--tier3-constraints]
phototag faces verify          [--min-score 0.65] [--min-area 1024] [--apply]
phototag faces refine-noise    [--min-size 3] [--min-samples 2] [--persist]
phototag faces auto-attach     [--threshold 0.5] [--auto-verify-threshold 0.7]
                               [--limit N] [--persist]
phototag faces name            CLUSTER_ID NAME
phototag faces unname          CLUSTER_ID
phototag faces clear-noise-labels
phototag faces corrections     [--action ACT] [--face-id N] [--limit N]
phototag faces corrections-compact [--apply]
phototag faces stats           [--per-identity]
phototag faces purge           [--keep-identities] [--yes]
```

## Performance expectations

| Hardware | Detect (face/photo) | Embed | Cluster (10k faces) |
|---|---|---|---|
| GPU 980 Ti | 4–8 img/s | 80–120 face/s | < 1 min |
| CPU 28-core | 0.5–1.5 img/s | 30–50 face/s | < 1 min |

For the current corpus (~9.4k photos, ~30–50% with faces): on CPU ~2 h, on 980 Ti ~25 min.

## Failure modes

- **Detector misses small/blurry faces.** Acceptable; RAM clustering still finds those photos via "person, group" tags.
- **Twin/sibling collisions.** ArcFace can confuse them; UMAP+HDBSCAN may merge clusters. Mitigated by HDBSCAN's `min_samples`; user can split via UI later.
- **Heavy filters / make-up / sunglasses** drop accuracy. Expected; tag the cluster as low-confidence in the UI when intra-cluster variance is high.
- **Aging children**: ArcFace is identity-stable; not perfect over years but generally fine for ages 5+.

## What this does NOT do

- No face *re-identification across libraries* (no global identity DB).
- No demographic inference (age/gender/race) — separate models, separate ethics, out of scope.
- No emotion / expression tagging — out of scope.
- No matching against missing-persons or law-enforcement databases — explicitly forbidden by the README.

## Roadmap fit

Lands in v2 after the search/rename/XMP work, parallel to category mapping. Not a v1 prerequisite.
