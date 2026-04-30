from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import xxhash

from .config import IMAGE_EXTENSIONS, RAW_EXTENSIONS

_HASH_CHUNK = 1 << 20


@dataclass(frozen=True)
class ScannedFile:
    path: Path
    size: int
    mtime: float


def iter_images(root: Path, *, include_raw: bool = False) -> Iterator[ScannedFile]:
    exts = IMAGE_EXTENSIONS | RAW_EXTENSIONS if include_raw else IMAGE_EXTENSIONS
    for p in root.resolve().rglob("*"):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        try:
            stat = p.stat()
        except OSError:
            continue
        yield ScannedFile(path=p, size=stat.st_size, mtime=stat.st_mtime)


def hash_file(path: Path) -> str:
    h = xxhash.xxh64()
    with path.open("rb") as f:
        while chunk := f.read(_HASH_CHUNK):
            h.update(chunk)
    return str(h.hexdigest())
