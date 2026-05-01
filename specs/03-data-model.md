# 03 — Data model

Single SQLite file. Portable, zero-dep, JSON1 enabled.

## Schema

```sql
-- v1
CREATE TABLE images (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    hash         TEXT NOT NULL,
    mtime        REAL NOT NULL,
    width        INTEGER,
    height       INTEGER,
    exif_json    TEXT,                    -- JSON1
    processed_at TEXT NOT NULL
);
CREATE INDEX idx_images_hash ON images(hash);

CREATE TABLE tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE INDEX idx_tags_name ON tags(name);

CREATE TABLE image_tags (
    image_id   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags(id),
    score      REAL NOT NULL,
    model_name TEXT NOT NULL,             -- e.g. "ram_plus_swin_large_14m"
    PRIMARY KEY (image_id, tag_id, model_name)
);
CREATE INDEX idx_image_tags_score ON image_tags(score);
CREATE INDEX idx_image_tags_tag   ON image_tags(tag_id);

-- v1
CREATE TABLE embeddings (
    image_id   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    model_name TEXT NOT NULL,             -- e.g. "open_clip_vit_l_14"
    dim        INTEGER NOT NULL,
    vector     BLOB NOT NULL,             -- float32 packed
    PRIMARY KEY (image_id, model_name)
);

CREATE TABLE cluster_runs (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    params_json TEXT NOT NULL              -- umap + hdbscan params + seeds
);

CREATE TABLE clusters (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER NOT NULL REFERENCES cluster_runs(id) ON DELETE CASCADE,
    label_auto TEXT,
    label_user TEXT,
    size       INTEGER NOT NULL
);

CREATE TABLE image_clusters (
    image_id   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    distance   REAL NOT NULL,
    PRIMARY KEY (image_id, cluster_id)
);
CREATE INDEX idx_image_clusters_cluster ON image_clusters(cluster_id);

-- v2 — face recognition (opt-in; see 15-faces.md). Schema initially landed
-- as v4; v5/v7 add per-face flags, v6 adds the audit trail, v8 segregates
-- geo tags.
CREATE TABLE faces (
    id           INTEGER PRIMARY KEY,
    image_id     INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    bbox_json    TEXT NOT NULL,
    det_score    REAL NOT NULL,
    embedding    BLOB NOT NULL,
    dim          INTEGER NOT NULL,
    model_name   TEXT NOT NULL,
    landmarks_json TEXT,
    verified       INTEGER,                    -- v5: 1 passed / 0 suspect / NULL untested
                                               --     (set by `phototag faces verify`)
    user_verified  INTEGER                     -- v7: 1 = user clicked validate;
                                               --     attach helper auto-sets when sim>=0.7;
                                               --     drives the green styling + dup-drop spare
);

-- v8: tag.kind separates geo facts (model_name LIKE 'geo_%') from model
-- predictions. NULL = legacy/label, 'label' for ML predictions, 'geo' for
-- reverse-geocoded. `phototag stats --kind label` excludes geo so a city
-- name doesn't drown a visual match with score=1.0.
ALTER TABLE tags ADD COLUMN kind TEXT;

-- v6: audit trail of every user-driven correction. Survives the deletion of
-- the face/image rows it describes (no FK on face_id/image_id on purpose).
-- Wiped by `phototag faces purge` unless --keep-identities is set.
-- `action` enum: 'named' | 'unassigned' | 'deleted' | 'verified' | 'unverified'.
-- The tier-2 sticky pass uses 'unassigned' rows as cannot-link constraints.
CREATE TABLE face_corrections (
    id          INTEGER PRIMARY KEY,
    face_id     INTEGER,
    image_id    INTEGER,
    action      TEXT NOT NULL,                 -- 'named' | 'unassigned' | 'deleted' | 'verified' | 'unverified'
    cluster_id  INTEGER,
    name        TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE face_runs (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    params_json TEXT NOT NULL
);
CREATE TABLE face_clusters (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES face_runs(id) ON DELETE CASCADE,
    cluster_no INTEGER NOT NULL,
    size INTEGER NOT NULL,
    label_user TEXT,
    label_auto TEXT
);
CREATE TABLE face_cluster_assignments (
    face_id       INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
    cluster_id    INTEGER NOT NULL REFERENCES face_clusters(id) ON DELETE CASCADE,
    distance      REAL NOT NULL,
    distance_kind TEXT,                     -- v9: 'euclidean_umap' | 'cosine_dist'
    PRIMARY KEY (face_id, cluster_id)
);
CREATE TABLE face_identities (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    centroid BLOB NOT NULL,
    dim INTEGER NOT NULL,
    n_samples INTEGER NOT NULL,
    -- v10: per-identity Welford running stats on the auto-attach cosine
    -- similarity. Used by `attach_face_to_best_identity` to widen the
    -- auto-validate band on high-variance identities.
    sim_n    INTEGER NOT NULL DEFAULT 0,
    sim_mean REAL    NOT NULL DEFAULT 0.0,
    sim_M2   REAL    NOT NULL DEFAULT 0.0
);

-- v11
CREATE TABLE categories (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE tag_category_map (
    -- UNIQUE on tag_id: a tag maps to at most one category. Re-mapping
    -- replaces the previous category via `ON CONFLICT(tag_id) DO UPDATE`.
    tag_id      INTEGER NOT NULL UNIQUE REFERENCES tags(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE
);

CREATE TABLE cluster_categories (
    -- Cluster→category override (highest precedence, beats tag rules).
    cluster_id  INTEGER NOT NULL UNIQUE REFERENCES face_clusters(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE
);
```

## Vector storage

`embeddings.vector` is a packed float32 buffer (`np.ndarray.tobytes()`). At ~1 KB per ViT-B/32 vector or ~3 KB per ViT-L/14, 100k images = 100–300 MB. Acceptable.

If `sqlite-vec` is installed, expose a virtual table `vec_embeddings` for `MATCH` queries — otherwise compute cosine in Python via numpy.

## Idempotence

Re-tagging is gated by `(hash, mtime)`. Path changes alone don't trigger re-tagging if hash matches an existing row — emit an `UPDATE images SET path = ?` instead.

`model_name` columns enable parallel result sets when the model version bumps (see `13-risks.md`, model versioning).

## Migrations

Schema migrations live inline in `phototag/store.py:MIGRATIONS` (a list of SQL strings; numbered v1–v11 in code comments). Each is applied atomically inside its own `executescript`; the `meta(key, value)` table records `schema_version`.

### v9 — `face_cluster_assignments.distance_kind`

```sql
ALTER TABLE face_cluster_assignments ADD COLUMN distance_kind TEXT;
UPDATE face_cluster_assignments SET distance_kind = 'euclidean_umap' WHERE distance_kind IS NULL;
```

`distance` historically mixed two scales: Euclidean distance in UMAP-reduced space (written by `cluster_faces` / `cluster_orphan_faces`) and `1.0 - cosine_sim` (written by manual / auto-attach paths). Sorting a mixed cluster was meaningless. `distance_kind` makes the scale explicit (`'euclidean_umap'` or `'cosine_dist'`) so the UI can format the value correctly. Pre-existing rows came from the UMAP path, so the backfill is safe.

### v10 — `face_identities` Welford stats

```sql
ALTER TABLE face_identities ADD COLUMN sim_n    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE face_identities ADD COLUMN sim_mean REAL    NOT NULL DEFAULT 0.0;
ALTER TABLE face_identities ADD COLUMN sim_M2   REAL    NOT NULL DEFAULT 0.0;
```

Per-identity running statistics on the auto-attach cosine similarity. Folded by `Store.record_identity_attach_sim` after each successful `attach_face_to_best_identity`. Used to widen the auto-validate band by `2σ` for high-variance identities (kid drift, lighting swings) — see `specs/16-improvement-plan.md` row #4.

### v11 — categories + tag/cluster mapping

```sql
CREATE TABLE categories          (id, name UNIQUE);
CREATE TABLE tag_category_map    (tag_id UNIQUE FK→tags, category_id FK→categories);
CREATE TABLE cluster_categories  (cluster_id UNIQUE FK→face_clusters, category_id FK→categories);
```

User category vocabulary + the two rule sources that drive `lr:HierarchicalSubject` in XMP sidecars (cluster wins over tag per `08-xmp-categories.md`). `Store.categories_for_image` resolves the union; `phototag xmp write --apply` materializes `category|subject` entries.
