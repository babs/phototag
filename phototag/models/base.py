from typing import Protocol

import numpy as np
from PIL import Image


class Tagger(Protocol):
    name: str

    def tag(self, images: list[Image.Image]) -> list[list[tuple[str, float]]]:
        """Return [(tag, score), ...] per image, already filtered by threshold."""
        ...


class Embedder(Protocol):
    name: str
    dim: int

    def embed_images(self, images: list[Image.Image]) -> np.ndarray:
        """Return (N, dim) float32 unit-normalized embeddings."""
        ...

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        """Return (N, dim) float32 unit-normalized embeddings."""
        ...
