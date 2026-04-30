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

-- v2
CREATE TABLE categories (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE tag_category_map (
    tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    PRIMARY KEY (tag_id, category_id)
);
```

## Vector storage

`embeddings.vector` is a packed float32 buffer (`np.ndarray.tobytes()`). At ~1 KB per ViT-B/32 vector or ~3 KB per ViT-L/14, 100k images = 100–300 MB. Acceptable.

If `sqlite-vec` is installed, expose a virtual table `vec_embeddings` for `MATCH` queries — otherwise compute cosine in Python via numpy.

## Idempotence

Re-tagging is gated by `(hash, mtime)`. Path changes alone don't trigger re-tagging if hash matches an existing row — emit an `UPDATE images SET path = ?` instead.

`model_name` columns enable parallel result sets when the model version bumps (see `13-risks.md`, model versioning).

## Migrations

Schema migrations: simple numbered SQL files in `phototag/migrations/`. Apply in order, store `schema_version` in a `meta(key, value)` table.
