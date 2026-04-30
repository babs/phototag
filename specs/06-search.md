# 06 — Semantic search (v1.5)

Free-text query → ranked images. Powered by CLIP embeddings already cached for clustering.

## Flow

1. Encode the query string with CLIP text encoder → 512/768-dim vector.
2. Compute cosine similarity against all `embeddings.vector` rows for the same model.
3. Return top-K paths + scores.

## Implementation paths

### Path A — pure numpy (default)

- Load all vectors into a single `(N, D)` float32 matrix at startup (lazy, cached).
- 100k × 512 float32 ≈ 200 MB RAM. Acceptable.
- Cosine via normalized matmul: `scores = M @ q`. Sub-second on consumer CPU for 100k vectors.

### Path B — `sqlite-vec` (optional)

- If `sqlite-vec` is loaded, expose `vec_embeddings` virtual table.
- Query: `SELECT image_id FROM vec_embeddings WHERE embedding MATCH ? ORDER BY distance LIMIT ?`.
- Pushes filter predicates into SQL (`WHERE` on tags, EXIF, cluster).

## Filters

Combine semantic score with metadata filters:

```
phototag query "x-ray of a hand" \
  --after 2020-01-01 \
  --tag medical \
  --cluster 7 \
  --limit 50
```

When filters are present, apply in SQL first (cheap), then re-rank by cosine on the reduced set.

## Score thresholds

CLIP cosine scores are not calibrated. Don't filter by absolute threshold — return top-K and let the user judge. Optionally show score in CLI output for transparency.

## Multi-modal queries (future)

Image-as-query (find similar to `--like path/to/img.jpg`): same flow, encode the image instead of text. Trivially supported once text version works.
