"""Fake `Tagger` / `Embedder` implementations for pipeline / clustering tests.

These honor `phototag.models.base.{Tagger, Embedder}` protocols without
needing the heavy ML dependencies (`[ram]` / `[clip]` extras). They give
deterministic outputs keyed on the image's pixel content so the same
photo always yields the same tags / embedding — i.e. `scan_and_tag` and
`embed_all` idempotence is verifiable.

Used by tests/test_pipeline.py (#29).
"""

import hashlib
from collections.abc import Mapping

import numpy as np
from PIL import Image


def _seed_from(img: Image.Image) -> int:
    """Stable 32-bit seed from the image's raw pixel bytes. Two PIL images
    with identical pixels produce the same seed (and thus the same fake
    tag set / embedding), which is what idempotence tests rely on."""
    raw = img.tobytes() + str(img.size).encode() + str(img.mode).encode()
    digest = hashlib.sha256(raw).digest()
    return int.from_bytes(digest[:4], "big")


class FakeTagger:
    """Deterministic tagger. Returns a small set of tags whose names + scores
    derive from the image content's hash, so:
      - identical images → identical tag rows
      - different images → different tag rows
      - threshold semantics are testable via the `threshold` constructor arg

    Optionally, callers can pass a `tag_overrides` mapping of
    `(width, height) → list[(name, score)]` to pin specific photos to a
    fixed tag set (handy when a test needs a known tag like "cat").
    """

    name = "fake_tagger_v1"

    def __init__(
        self,
        *,
        threshold: float = 0.5,
        tag_overrides: Mapping[tuple[int, int], list[tuple[str, float]]] | None = None,
    ) -> None:
        self.threshold = float(threshold)
        self._overrides = dict(tag_overrides or {})

    def tag(self, images: list[Image.Image]) -> list[list[tuple[str, float]]]:
        out: list[list[tuple[str, float]]] = []
        for img in images:
            override = self._overrides.get((img.width, img.height))
            if override is not None:
                out.append([(n, float(s)) for n, s in override if s >= self.threshold])
                continue
            seed = _seed_from(img)
            rng = np.random.default_rng(seed)
            # Pick 3 tags from a tiny vocabulary; scores in [0.5, 1.0).
            vocab = ["scene", "indoor", "outdoor", "person", "object", "landscape", "doc"]
            chosen = rng.choice(vocab, size=3, replace=False).tolist()
            scores = rng.uniform(0.5, 1.0, size=3).round(3).tolist()
            tags = [
                (str(name), float(score))
                for name, score in zip(chosen, scores, strict=True)
                if score >= self.threshold
            ]
            out.append(tags)
        return out


class FakeEmbedder:
    """Deterministic embedder producing unit-normalized vectors of `dim`.
    Same image → same vector, exactly as the real `ClipEmbedder` contract
    requires. Vectors are L2-normalized so a cosine similarity computation
    matches what `Store.load_embeddings` consumers expect.
    """

    def __init__(self, *, dim: int = 16) -> None:
        self.name = "fake_clip_v1"
        self.dim = int(dim)

    def _vec(self, seed: int) -> np.ndarray:
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(self.dim).astype(np.float32)
        n = float(np.linalg.norm(v))
        return v / max(n, 1e-12)

    def embed_images(self, images: list[Image.Image]) -> np.ndarray:
        if not images:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack([self._vec(_seed_from(img)) for img in images])

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        # Hash the string the same way image bytes are hashed so a text
        # query maps deterministically; CLIP doesn't actually share a
        # space with our random vectors, but for tests the determinism
        # is what matters.
        seeds = [int.from_bytes(hashlib.sha256(t.encode()).digest()[:4], "big") for t in texts]
        return np.vstack([self._vec(s) for s in seeds])
