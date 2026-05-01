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
-- v2
CREATE TABLE faces (
    id           INTEGER PRIMARY KEY,
    image_id     INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    bbox_json    TEXT NOT NULL,            -- [x, y, w, h] in original-pixel coords
    det_score    REAL NOT NULL,            -- detector confidence
    embedding    BLOB NOT NULL,            -- float32 packed
    dim          INTEGER NOT NULL,
    model_name   TEXT NOT NULL,            -- e.g. "insightface_buffalo_l_v1"
    landmarks_json TEXT                    -- 5 keypoints [[x,y]*5], optional
);
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
    face_id    INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
    cluster_id INTEGER NOT NULL REFERENCES face_clusters(id) ON DELETE CASCADE,
    distance   REAL NOT NULL,
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
phototag faces detect [--gpu] [--limit N]
```

For each image without a face row:
1. Decode → BGR.
2. RetinaFace pass: list of `(bbox, det_score, landmarks)`.
3. For each face: align via 5-point landmarks, ArcFace embedding (L2-normalized).
4. Persist all faces in one transaction per image.

Idempotent: skip if `faces` table already has rows for the image and the model name matches.

Decode is reusable from `pipeline._open_image`; the existing prefetch queue applies.

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
GET  /api/people                          → [{id, name, sample_face_ids[3], n_photos}]
GET  /api/people/{cluster_id}             → cluster + member faces (image_id, bbox)
POST /api/people/{cluster_id}/name        → {"name": "Anne"} → label_user
GET  /api/images/{id}/faces               → faces in this image, with cluster + name
GET  /face-thumb/{face_id}                → server-cropped + aligned face JPEG
```

## UI

### People panel

A new sidebar tab next to "clusters" called **people** lists face clusters by size (or alphabetically when named). Click → person page: face-crop thumbnails on top, full photos containing that person below.

### Lightbox face overlay (this turn's idea)

When a single image is open in the lightbox:
- Fetch `/api/images/{id}/faces`.
- For each face, render an absolutely-positioned `<div>` overlay matching the bbox, scaled to the displayed image size. Listen on `img.onload` and recompute on resize.
- Style: 2 px border in a stable per-cluster colour (hash cluster_id → hue), name floating below the box.
- Click an overlay → either:
  - if cluster has `label_user`: load `/api/people/{cluster_id}` (jump to that person)
  - else: open a tiny inline form to name the cluster ("name this person…")
- Toggle overlay visibility with **F** key or a button in the lightbox toolbar; default on.

This makes naming a face a one-click flow: see a face → click → type name → it propagates to every other photo of that person, immediately.

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
5. **Wipe is total.** `phototag faces purge` drops every faces-related table; `--keep-identities` to keep names but drop embeddings.
6. **Don't process other people's libraries** without their consent. The README states this explicitly.

## CLI summary

```
phototag faces detect [--gpu] [--limit N] [--i-understand]
phototag faces cluster [--min-size 3] [--min-samples 2]
phototag faces name CLUSTER_ID NAME
phototag faces unname CLUSTER_ID
phototag faces purge [--keep-identities]
phototag faces report [--out report-faces/]
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
