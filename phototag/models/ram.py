from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


def _resolve_device(device: str) -> str:
    import torch

    if device == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    return device


class RamTagger:
    """RAM++ wrapper. Heavy imports are local so the CLI works without the [ram] extra."""

    name = "ram_plus_swin_large_14m"

    def __init__(
        self,
        models_dir: Path,
        *,
        image_size: int = 384,
        threshold: float = 0.68,
        device: str = "auto",
    ) -> None:
        # RAM imports `apply_chunking_to_forward` etc. from `transformers.modeling_utils`,
        # but they moved to `transformers.pytorch_utils` in transformers >=4.30. Re-export
        # them at the old location so RAM's bert.py can import without modification.
        import transformers.modeling_utils as _mu
        import transformers.pytorch_utils as _pu

        for _name in ("apply_chunking_to_forward", "find_pruneable_heads_and_indices", "prune_linear_layer"):
            if not hasattr(_mu, _name) and hasattr(_pu, _name):
                setattr(_mu, _name, getattr(_pu, _name))

        import torch
        from ram import get_transform
        from ram.models import ram_plus

        models_dir.mkdir(parents=True, exist_ok=True)
        weights = models_dir / "ram_plus_swin_large_14m.pth"
        if not weights.exists():
            raise FileNotFoundError(
                f"RAM++ weights not found at {weights}. Download from "
                "https://github.com/xinyu1205/recognize-anything (ram_plus_swin_large_14m.pth)."
            )
        self.image_size = image_size
        self.threshold = threshold
        self.device = _resolve_device(device)
        self.transform = get_transform(image_size=image_size)
        model = ram_plus(pretrained=str(weights), image_size=image_size, vit="swin_l")
        model.eval()
        self.model = model.to(self.device)
        self._torch = torch

    def tag(self, images: list[Image.Image]) -> list[list[tuple[str, float]]]:
        if not images:
            return []
        torch = self._torch
        F = torch.nn.functional
        batch = torch.stack([self.transform(img.convert("RGB")) for img in images]).to(
            self.device, non_blocking=True
        )
        m = self.model
        # Mirror generate_tag but expose sigmoid(logits) so we keep per-class scores.
        with torch.no_grad():
            image_embeds = m.image_proj(m.visual_encoder(batch))
            image_atts = torch.ones(image_embeds.size()[:-1], dtype=torch.long, device=self.device)
            image_cls = image_embeds[:, 0, :]
            bs = image_embeds.size(0)
            des_per_class = int(m.label_embed.shape[0] / m.num_class)

            image_cls = image_cls / image_cls.norm(dim=-1, keepdim=True)
            reweight_scale = m.reweight_scale.exp()
            logits_per_image = (reweight_scale * image_cls @ m.label_embed.t()).view(bs, -1, des_per_class)
            weight_normalized = F.softmax(logits_per_image, dim=2)
            label_embed_reweight = torch.empty(
                bs, m.num_class, 512, device=self.device, dtype=image_embeds.dtype
            )
            reshaped_value = m.label_embed.view(-1, des_per_class, 512)
            for i in range(bs):
                product = weight_normalized[i].unsqueeze(-1) * reshaped_value
                label_embed_reweight[i] = product.sum(dim=1)

            label_embed = F.relu(m.wordvec_proj(label_embed_reweight))
            tagging_embed = m.tagging_head(
                encoder_embeds=label_embed,
                encoder_hidden_states=image_embeds,
                encoder_attention_mask=image_atts,
                return_dict=False,
                mode="tagging",
            )
            logits = m.fc(tagging_embed[0]).squeeze(-1)
            probs = torch.sigmoid(logits).cpu().numpy()
            # RAM masks redundant-class indices via `delete_tag_index`.
            probs[:, m.delete_tag_index] = 0.0

        tag_list: list[Any] = list(m.tag_list)
        out: list[list[tuple[str, float]]] = []
        for row in probs:
            keep = np.where(row >= self.threshold)[0]
            scored = [(str(tag_list[int(i)]), float(row[int(i)])) for i in keep]
            scored.sort(key=lambda kv: kv[1], reverse=True)
            out.append(scored)
        return out
