from dataclasses import dataclass

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic", ".heif", ".bmp", ".gif"}
)

RAW_EXTENSIONS: frozenset[str] = frozenset({".cr2", ".cr3", ".nef", ".arw", ".raf", ".dng", ".orf", ".rw2"})


@dataclass(frozen=True)
class RamConfig:
    image_size: int = 384
    threshold: float = 0.68
    name: str = "ram_plus_swin_large_14m"


@dataclass(frozen=True)
class ClipConfig:
    model_name: str = "ViT-B-32"
    pretrained: str = "laion2b_s34b_b79k"


@dataclass(frozen=True)
class ClusterConfig:
    umap_n_components: int = 50
    umap_n_neighbors: int = 30
    umap_min_dist: float = 0.0
    umap_metric: str = "cosine"
    hdbscan_min_cluster_size: int = 20
    hdbscan_min_samples: int = 5
    random_state: int = 42
