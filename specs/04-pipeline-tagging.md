# 04 — Tagging pipeline (v1)

## Steps

1. **Scan** — recursive walk; filter by extension: `.jpg`, `.jpeg`, `.png`, `.heic`, `.tif`, `.tiff`, `.webp`, plus RAW (`.cr2`, `.nef`, `.arw`, `.raf`, `.dng`) decoded via `rawpy`.
2. **Hash + skip** — `(hash, mtime)` already in DB and unchanged → skip.
3. **Preprocess** — decode → sRGB → resize 384×384 (RAM++ input).
4. **RAM++ inference** — returns `[(tag, score)]`. Threshold default `0.68`, configurable.
5. **EXIF extraction** — date, GPS, camera → enriches `images.exif_json`.
6. **Persist** — tags + scores + metadata in SQLite (one transaction per batch).

## Batching

- Inference: 8–32 images per batch (GPU-bound). Batch size auto-detected from VRAM, override via `--batch-size`.
- I/O (decode + hash): multiprocess pool sized to CPU count.
- Producer/consumer: I/O pool feeds a bounded queue; GPU worker drains it.

## Hashing

`xxhash` or `blake3` over file bytes. Used for change detection — not cryptographic. Hash + mtime together: hash detects content change, mtime catches the rare case of zero-byte truncation matching an empty hash.

## Threshold tuning

RAM scores aren't calibrated probabilities. Default 0.68 is the upstream recommendation. Provide `--threshold` flag; allow per-tag override later if needed (`tag_thresholds.json`). Always store the raw score so threshold can be re-applied without re-running inference.

## Idempotence

Re-running `scan` on the same folder is a no-op for unchanged files. Force re-tag with `--force` (rehash all) or `--force-tag` (keep hash, rerun inference — useful when bumping RAM version).

## Error handling

- Decode failure → log + skip, mark `images.exif_json` with `{"error": "..."}`.
- Inference OOM → halve batch size, retry once, then crash loudly.
- Disk full / DB locked → fail fast, no silent retry.

## Observability

- Per-run: count scanned / skipped / tagged / failed.
- Tail log to stderr; `--verbose` for per-image events.
- Optional `--progress` shows tqdm bar.
