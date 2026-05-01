# 04 — Tagging pipeline (v1)

## Steps

1. **Scan** — recursive walk; filter by extension: `.jpg`, `.jpeg`, `.png`, `.heic`, `.tif`, `.tiff`, `.webp`, plus RAW (`.cr2`, `.nef`, `.arw`, `.raf`, `.dng`) decoded via `rawpy`.
2. **Hash + skip** — `(hash, mtime)` already in DB and unchanged → skip.
3. **Preprocess** — decode → sRGB → resize 384×384 (RAM++ input).
4. **RAM++ inference** — returns `[(tag, score)]`. Threshold default `0.68`, configurable.
5. **EXIF extraction** — date, GPS, camera → enriches `images.exif_json`.
6. **Persist** — tags + scores + metadata in SQLite (one transaction per batch).

## Batching

- Inference: 8–32 images per batch (GPU-bound). Default `--batch-size 16`; tune via the flag (`scan --batch-size N`).
- I/O (decode + hash): `ThreadPoolExecutor` sized to `decode_workers`
  (default = `max(2, batch_size)`; override via `--decode-workers` on
  `scan`/`embed`). Threads suffice because the hot path is PIL +
  xxhash, both releasing the GIL on file/CPU-bound work — multiprocess
  overhead is unjustified on a single-machine corpus.
- Producer/consumer: a background producer thread fills a bounded
  queue (`queue_depth=2` batches) so decode overlaps the GPU forward —
  observed ~50 % → ~100 % GPU duty cycle on the legacy 980 Ti when
  prefetching is active.

## Hashing

`xxhash` over file bytes (`_HASH_CHUNK = 1 MiB` reads). Used for change detection — not cryptographic. Hash + mtime together: hash detects content change, mtime catches the rare case of zero-byte truncation matching an empty hash.

## Threshold tuning

RAM scores aren't calibrated probabilities. Default 0.68 is the upstream recommendation. Provide `--threshold` flag; allow per-tag override later if needed (`tag_thresholds.json`). Always store the raw score so threshold can be re-applied without re-running inference.

## Idempotence

Re-running `scan` on the same folder is a no-op for unchanged files. Force re-tag with `--force` (rehash all) or `--force-tag` (keep hash, rerun inference — useful when bumping RAM version).

## Error handling

- Decode failure → log `decode_failed` (path + error) and skip the image; the row is not upserted, the `failed` counter increments. We do NOT write a synthetic exif_json with the error — failed rows simply don't exist in the DB until a re-scan succeeds.
- Inference failure (incl. OOM) → log `tag_batch_failed`, count the whole batch as failed, continue with the next batch. There is no batch-size halving / retry today; a hard OOM crashes the worker and the user re-runs with `--batch-size N/2` manually.
- Disk full / DB locked → SQLite raises (5 s `busy_timeout` absorbs brief contention); we propagate, no silent retry.
- Decoder thread errors → caught in `pipeline._prefetch_decoded_batches.producer` and logged as `decode_pipeline_failed`; the consumer sees `_END` so the run finishes with the partial counts instead of hanging.

## Observability

- Per-run: counts of `scanned / skipped / tagged / failed` emitted as `scan_completed` (structlog). Same shape for `embed_all` → `embed_completed`.
- Tail logs to stderr/stdout via structlog; TTY → console renderer, otherwise JSON.
- No tqdm — progress is implied by the `scan_filter` / `decode_pipeline_failed` / `embed_started` events. A progress bar would clutter the JSON log channel and isn't worth the dependency for a one-shot CLI.
