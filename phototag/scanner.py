import os
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
    # os.walk(followlinks=False) avoids infinite recursion on symlink cycles
    # (rglob follows symlinks unconditionally on Python <3.13).
    # Don't `resolve()` the root: the corpus root is typically a symlink
    # (e.g. `data/pictures` → user's library) and we want yielded paths to
    # keep the symlinked prefix so they relativize cleanly against the DB
    # parent in `Store.relative_path`.
    root_walk = root if root.is_absolute() else root.absolute()
    for dirpath, _dirnames, filenames in os.walk(root_walk, followlinks=False):
        for name in filenames:
            if Path(name).suffix.lower() not in exts:
                continue
            p = Path(dirpath) / name
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
