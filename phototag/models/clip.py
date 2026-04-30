from pathlib import Path

import numpy as np
from PIL import Image


def _resolve_device(device: str) -> str:
    import torch

    if device == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    return device


class ClipEmbedder:
    """open_clip wrapper. Returns L2-normalized float32 vectors."""

    def __init__(
        self,
        models_dir: Path,
        *,
        model_name: str = "ViT-B-32",
        pretrained: str = "laion2b_s34b_b79k",
        device: str = "auto",
    ) -> None:
        import open_clip
        import torch

        models_dir.mkdir(parents=True, exist_ok=True)
        self.device = _resolve_device(device)
        model, _, preprocess = open_clip.create_model_and_transforms(
            model_name, pretrained=pretrained, cache_dir=str(models_dir)
        )
        model.eval()
        self.model = model.to(self.device)
        self.preprocess = preprocess
        self.tokenizer = open_clip.get_tokenizer(model_name)
        self.name = f"open_clip_{model_name}_{pretrained}".replace("/", "_")
        with torch.no_grad():
            dummy = torch.zeros(1, 3, 224, 224, device=self.device)
            self.dim = int(self.model.encode_image(dummy).shape[-1])
        self._torch = torch

    def embed_images(self, images: list[Image.Image]) -> np.ndarray:
        if not images:
            return np.zeros((0, self.dim), dtype=np.float32)
        torch = self._torch
        batch = torch.stack([self.preprocess(img.convert("RGB")) for img in images]).to(self.device)
        with torch.no_grad():
            feats = self.model.encode_image(batch)
            feats = feats / feats.norm(dim=-1, keepdim=True).clamp_min(1e-12)
        return feats.detach().cpu().numpy().astype(np.float32, copy=False)

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        torch = self._torch
        tokens = self.tokenizer(texts).to(self.device)
        with torch.no_grad():
            feats = self.model.encode_text(tokens)
            feats = feats / feats.norm(dim=-1, keepdim=True).clamp_min(1e-12)
        return feats.detach().cpu().numpy().astype(np.float32, copy=False)
