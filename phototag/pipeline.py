import queue
import threading
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from .exif import extract_exif
from .logging import get_logger
from .models.base import Embedder, Tagger
from .scanner import ScannedFile, hash_file, iter_images
from .store import Store

# Sentinel value pushed onto the prefetch queue to signal end-of-stream.
_END = object()
# Prepared item: (file, hash_or_none, image_or_none).
PreparedItem = tuple[ScannedFile, str | None, Image.Image | None]

log = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _open_image(path: Path) -> Image.Image | None:
    """Decode + close-handle. See faces._open_image — same logic, kept here to
    avoid an import cycle (pipeline → faces would pull insightface lazily)."""
    try:
        with Image.open(path) as img:
            img.load()
            return img.copy()
    except Exception as e:
        log.warning("decode_failed", path=str(path), error=str(e))
        return None


def _batched(it: Iterable[ScannedFile], n: int) -> Iterator[list[ScannedFile]]:
    chunk: list[ScannedFile] = []
    for x in it:
        chunk.append(x)
        if len(chunk) >= n:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _decode_one(sf: ScannedFile) -> PreparedItem:
    """Hash + decode a single file. Returns (sf, hash, img) or (sf, hash, None) on failure."""
    h = hash_file(sf.path)
    img = _open_image(sf.path)
    return sf, h, img


def _prefetch_decoded_batches(
    files: list[ScannedFile],
    batch_size: int,
    *,
    workers: int,
    queue_depth: int = 2,
) -> Iterator[list[PreparedItem]]:
    """Decode batches in worker threads, hand them off via a bounded queue.

    Why: the GPU is idle while the next batch is being read+decoded. Prefetching
    overlaps CPU decode with GPU forward and roughly doubles throughput on this
    hardware (980 Ti, swin_l), as observed by ~50% GPU duty cycle without it.
    """
    if not files:
        return
    q: queue.Queue[list[PreparedItem] | object] = queue.Queue(maxsize=queue_depth)

    def producer() -> None:
        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for batch_files in _batched(iter(files), batch_size):
                    q.put(list(ex.map(_decode_one, batch_files)))
        except Exception as e:
            # Without this, a decode/hash failure in the pool dies silently and
            # the consumer reports "success" with bogus counts.
            log.error("decode_pipeline_failed", error=str(e))
        finally:
            q.put(_END)

    t = threading.Thread(target=producer, daemon=True)
    t.start()
    try:
        while True:
            item = q.get()
            if item is _END:
                return
            yield item  # type: ignore[misc]
    finally:
        t.join(timeout=5.0)
        if t.is_alive():
            log.warning("decode_producer_did_not_exit", timeout_s=5.0)


def scan_and_tag(
    root: Path,
    store: Store,
    tagger: Tagger,
    *,
    batch_size: int = 16,
    force: bool = False,
    force_tag: bool = False,
    decode_workers: int | None = None,
) -> dict[str, int]:
    """Scan + hash + tag. Idempotent on (path, hash, mtime).

    Decoding+hashing runs in a background thread pool so the GPU stays busy.
    """
    counts = {"scanned": 0, "skipped": 0, "tagged": 0, "failed": 0}
    files = list(iter_images(root))
    counts["scanned"] = len(files)
    log.info("scan_started", root=str(root), found=len(files))

    # Cheap pass: drop files unchanged-by-mtime so we don't even decode them.
    if force or force_tag:
        to_decode = files
    else:
        to_decode = []
        for sf in files:
            existing = store.get_image_by_path(str(sf.path))
            if existing is not None and existing.mtime == sf.mtime:
                counts["skipped"] += 1
            else:
                to_decode.append(sf)
    log.info("scan_filter", to_decode=len(to_decode), skipped=counts["skipped"])

    workers = decode_workers if decode_workers is not None else max(2, batch_size)
    for batch in _prefetch_decoded_batches(to_decode, batch_size, workers=workers):
        to_infer: list[tuple[int, Image.Image]] = []
        with store.transaction():
            for sf, content_hash, img in batch:
                if img is None or content_hash is None:
                    counts["failed"] += 1
                    continue
                spath = str(sf.path)
                existing = store.get_image_by_path(spath)
                if (
                    existing is not None
                    and existing.hash == content_hash
                    and existing.mtime == sf.mtime
                    and not (force or force_tag)
                ):
                    counts["skipped"] += 1
                    continue
                w, h = img.size
                # Cheap once we already have the file open in memory; cluster naming
                # and date filters benefit from EXIF being present from scan time.
                exif = extract_exif(sf.path)
                image_id = store.upsert_image(
                    path=spath,
                    hash_=content_hash,
                    mtime=sf.mtime,
                    width=w,
                    height=h,
                    exif=exif,
                    processed_at=_now_iso(),
                )
                to_infer.append((image_id, img))

        if not to_infer:
            continue

        try:
            tag_results = tagger.tag([img for _, img in to_infer])
        except Exception as e:
            log.error("tag_batch_failed", error=str(e), n=len(to_infer))
            counts["failed"] += len(to_infer)
            continue

        with store.transaction():
            for (image_id, _img), tags in zip(to_infer, tag_results, strict=True):
                store.replace_image_tags(image_id, tagger.name, tags)
                counts["tagged"] += 1

    log.info("scan_completed", **counts)
    return counts


def embed_all(
    store: Store,
    embedder: Embedder,
    *,
    batch_size: int = 32,
    force: bool = False,
    decode_workers: int | None = None,
) -> dict[str, int]:
    """Compute CLIP embeddings for all images that don't have them yet.

    Decoding runs in worker threads to keep the GPU busy.
    """
    counts = {"total": 0, "embedded": 0, "skipped": 0, "failed": 0}
    images = list(store.iter_images())
    counts["total"] = len(images)
    log.info("embed_started", total=len(images), model=embedder.name)

    todo = []
    for row in images:
        if not force and store.has_embedding(row.id, embedder.name):
            counts["skipped"] += 1
            continue
        todo.append(row)

    workers = decode_workers if decode_workers is not None else max(2, batch_size // 2)
    q: queue.Queue[list[tuple[int, Image.Image | None]] | object] = queue.Queue(maxsize=2)

    def producer() -> None:
        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for chunk_start in range(0, len(todo), batch_size):
                    chunk = todo[chunk_start : chunk_start + batch_size]
                    decoded = list(ex.map(lambda r: (r.id, _open_image(Path(r.path))), chunk))
                    q.put(decoded)
        except Exception as e:
            log.error("embed_decode_failed", error=str(e))
        finally:
            q.put(_END)

    t = threading.Thread(target=producer, daemon=True)
    t.start()
    try:
        while True:
            item = q.get()
            if item is _END:
                break
            assert isinstance(item, list)
            loaded: list[tuple[int, Image.Image]] = []
            for image_id, img in item:
                if img is None:
                    counts["failed"] += 1
                else:
                    loaded.append((image_id, img))
            if not loaded:
                continue
            try:
                vecs = embedder.embed_images([img for _, img in loaded])
            except Exception as e:
                log.error("embed_batch_failed", error=str(e), n=len(loaded))
                counts["failed"] += len(loaded)
                continue
            with store.transaction():
                for (image_id, _img), vec in zip(loaded, vecs, strict=True):
                    store.upsert_embedding(image_id, embedder.name, vec)
                    counts["embedded"] += 1
    finally:
        t.join(timeout=5.0)
        if t.is_alive():
            log.warning("embed_producer_did_not_exit", timeout_s=5.0)
    log.info("embed_completed", **counts)
    return counts
