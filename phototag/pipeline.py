from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

from .logging import get_logger
from .scanner import ScannedFile, hash_file, iter_images
from .store import Store

if TYPE_CHECKING:
    from .models.base import Embedder, Tagger

log = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _open_image(path: Path) -> Image.Image | None:
    try:
        img = Image.open(path)
        img.load()
        return img
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


def scan_and_tag(
    root: Path,
    store: Store,
    tagger: Tagger,
    *,
    batch_size: int = 16,
    force: bool = False,
    force_tag: bool = False,
) -> dict[str, int]:
    """Scan + hash + tag. Idempotent on (path, hash, mtime)."""
    counts = {"scanned": 0, "skipped": 0, "tagged": 0, "failed": 0}
    files = list(iter_images(root))
    counts["scanned"] = len(files)
    log.info("scan_started", root=str(root), found=len(files))

    for batch in _batched(iter(files), batch_size):
        to_infer: list[tuple[int, Image.Image]] = []
        with store.transaction():
            for sf in batch:
                spath = str(sf.path)
                existing = store.get_image_by_path(spath)
                unchanged = existing is not None and existing.mtime == sf.mtime and not (force or force_tag)
                if unchanged:
                    counts["skipped"] += 1
                    continue
                content_hash = hash_file(sf.path)
                if (
                    existing is not None
                    and existing.hash == content_hash
                    and existing.mtime == sf.mtime
                    and not (force or force_tag)
                ):
                    counts["skipped"] += 1
                    continue
                img = _open_image(sf.path)
                if img is None:
                    counts["failed"] += 1
                    continue
                w, h = img.size
                image_id = store.upsert_image(
                    path=spath,
                    hash_=content_hash,
                    mtime=sf.mtime,
                    width=w,
                    height=h,
                    exif=None,
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
) -> dict[str, int]:
    """Compute CLIP embeddings for all images that don't have them yet."""
    counts = {"total": 0, "embedded": 0, "skipped": 0, "failed": 0}
    images = list(store.iter_images())
    counts["total"] = len(images)
    log.info("embed_started", total=len(images), model=embedder.name)

    for chunk_start in range(0, len(images), batch_size):
        chunk = images[chunk_start : chunk_start + batch_size]
        loaded: list[tuple[int, Image.Image]] = []
        for row in chunk:
            if not force and store.has_embedding(row.id, embedder.name):
                counts["skipped"] += 1
                continue
            img = _open_image(Path(row.path))
            if img is None:
                counts["failed"] += 1
                continue
            loaded.append((row.id, img))

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
    log.info("embed_completed", **counts)
    return counts
