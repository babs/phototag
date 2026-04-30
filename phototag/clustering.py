import math
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import numpy as np

from .config import ClusterConfig
from .logging import get_logger
from .store import Store

log = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _tfidf_labels(cluster_tags: dict[int, list[list[str]]], *, top_k: int = 5) -> dict[int, list[str]]:
    """TF-IDF over cluster tag bags."""
    df: Counter[str] = Counter()
    for bags in cluster_tags.values():
        seen: set[str] = set()
        for tags in bags:
            for t in tags:
                if t not in seen:
                    df[t] += 1
                    seen.add(t)
    n_clusters = max(1, len(cluster_tags))
    out: dict[int, list[str]] = {}
    for cid, bags in cluster_tags.items():
        tf: Counter[str] = Counter()
        for tags in bags:
            for t in tags:
                tf[t] += 1
        if not tf:
            out[cid] = []
            continue
        scored: list[tuple[str, float]] = []
        for tag, freq in tf.items():
            idf = math.log((n_clusters + 1) / (df[tag] + 1)) + 1.0
            scored.append((tag, freq * idf))
        scored.sort(key=lambda kv: kv[1], reverse=True)
        out[cid] = [t for t, _ in scored[:top_k]]
    return out


def cluster(
    store: Store,
    *,
    embedder_name: str,
    config: ClusterConfig | None = None,
) -> int:
    """Run UMAP+HDBSCAN over stored embeddings, persist a new cluster_run."""
    cfg = config or ClusterConfig()
    image_ids, vectors = store.load_embeddings(embedder_name)
    if vectors.shape[0] < cfg.hdbscan_min_cluster_size:
        raise ValueError(
            f"Not enough embeddings ({vectors.shape[0]}) for min_cluster_size={cfg.hdbscan_min_cluster_size}"
        )
    log.info("cluster_started", n=int(vectors.shape[0]), dim=int(vectors.shape[1]))

    import hdbscan
    import umap

    n_components = min(cfg.umap_n_components, max(2, vectors.shape[0] - 2))
    n_neighbors = min(cfg.umap_n_neighbors, max(2, vectors.shape[0] - 1))
    reducer = umap.UMAP(
        n_components=n_components,
        n_neighbors=n_neighbors,
        min_dist=cfg.umap_min_dist,
        metric=cfg.umap_metric,
        random_state=cfg.random_state,
    )
    reduced = reducer.fit_transform(vectors)

    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=cfg.hdbscan_min_cluster_size,
        min_samples=cfg.hdbscan_min_samples,
        metric="euclidean",
        cluster_selection_method="eom",
    )
    labels = clusterer.fit_predict(reduced)

    params: dict[str, Any] = {
        "embedder": embedder_name,
        "n_vectors": int(vectors.shape[0]),
        "umap": {
            "n_components": n_components,
            "n_neighbors": n_neighbors,
            "min_dist": cfg.umap_min_dist,
            "metric": cfg.umap_metric,
            "random_state": cfg.random_state,
        },
        "hdbscan": {
            "min_cluster_size": cfg.hdbscan_min_cluster_size,
            "min_samples": cfg.hdbscan_min_samples,
            "metric": "euclidean",
            "selection": "eom",
        },
    }

    members: dict[int, list[tuple[int, np.ndarray]]] = {}
    for image_id, label, vec in zip(image_ids, labels, reduced, strict=True):
        members.setdefault(int(label), []).append((image_id, vec))

    per_image_tags = store.tags_per_image(image_ids)
    cluster_tags = {
        lv: [per_image_tags.get(iid, []) for iid, _ in m] for lv, m in members.items() if lv != -1
    }
    labels_auto = _tfidf_labels(cluster_tags, top_k=5)

    centroids = {lv: np.vstack([v for _, v in m]).mean(axis=0) for lv, m in members.items() if lv != -1}

    with store.transaction():
        run_id = store.create_cluster_run(params, _now_iso())
        for label_val in sorted(members.keys()):
            m = members[label_val]
            label_auto = ", ".join(labels_auto.get(label_val, [])) if label_val != -1 else "noise"
            cid = store.add_cluster(
                run_id=run_id,
                cluster_no=label_val,
                size=len(m),
                label_auto=label_auto or None,
            )
            if label_val == -1:
                for image_id, _vec in m:
                    store.assign_image_to_cluster(image_id, cid, distance=0.0)
            else:
                centroid = centroids[label_val]
                for image_id, vec in m:
                    d = float(np.linalg.norm(vec - centroid))
                    store.assign_image_to_cluster(image_id, cid, distance=d)

    log.info(
        "cluster_completed",
        run_id=run_id,
        n_clusters=sum(1 for k in members if k != -1),
        n_noise=len(members.get(-1, [])),
    )
    return run_id
