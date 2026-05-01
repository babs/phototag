"""Face detection, embedding, and clustering.

Heavy imports (insightface, cv2, hdbscan, umap) stay local so the core CLI
works without the [face] extra. See specs/15-faces.md for the design.
"""

import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from .logging import get_logger
from .store import Store

log = get_logger(__name__)

MODEL_NAME = "insightface_buffalo_l_v1"
EMBED_DIM = 512
# Detection input is resized to this max side. Bboxes are stored in this same
# coord space so they line up directly with the lightbox's /preview/{id} image
# (which is also clamped to 1280 px). Faces smaller than ~30 px in this space
# will be dropped by RetinaFace, which is fine for ~99 % of phone shots.
DETECT_MAX_SIDE = 1280


@dataclass(frozen=True)
class DetectedFace:
    bbox: list[int]  # [x, y, w, h] in original-pixel coords
    det_score: float
    embedding: np.ndarray
    landmarks: list[list[float]] | None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class FaceDetector:
    """RetinaFace + ArcFace via insightface buffalo_l."""

    name = MODEL_NAME

    def __init__(self, models_dir: Path, *, device: str = "auto") -> None:
        from insightface.app import FaceAnalysis

        models_dir.mkdir(parents=True, exist_ok=True)
        providers = self._providers(device)
        # buffalo_l ships RetinaFace det + ArcFace recognition; ~200 MB on first download.
        self.app = FaceAnalysis(
            name="buffalo_l",
            root=str(models_dir / "insightface"),
            providers=providers,
            allowed_modules=["detection", "recognition"],
        )
        ctx_id = 0 if "CUDAExecutionProvider" in providers else -1
        self.app.prepare(ctx_id=ctx_id, det_size=(640, 640))

    @staticmethod
    def _providers(device: str) -> list[str]:
        import onnxruntime as ort

        avail = ort.get_available_providers()
        want_gpu = device in ("auto", "cuda")
        if want_gpu and "CUDAExecutionProvider" in avail:
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        return ["CPUExecutionProvider"]

    def detect(self, img: Image.Image) -> list[DetectedFace]:
        # insightface expects BGR uint8. Resize first; on a 4 K JPEG this is
        # ~4× faster end-to-end and keeps bboxes in the same coord space as
        # the lightbox preview, so the overlay JS can use them as-is.
        rgb = ImageOps.exif_transpose(img).convert("RGB")
        rgb.thumbnail((DETECT_MAX_SIDE, DETECT_MAX_SIDE))
        arr = np.array(rgb)[:, :, ::-1]  # RGB -> BGR
        faces = self.app.get(arr)
        out: list[DetectedFace] = []
        for f in faces:
            x1, y1, x2, y2 = f.bbox.astype(int).tolist()
            bbox = [int(x1), int(y1), int(x2 - x1), int(y2 - y1)]
            kps = f.kps.tolist() if getattr(f, "kps", None) is not None else None
            emb = f.normed_embedding.astype(np.float32, copy=False)
            out.append(
                DetectedFace(
                    bbox=bbox,
                    det_score=float(f.det_score),
                    embedding=emb,
                    landmarks=kps,
                )
            )
        return out


def _open_image(path: Path) -> Image.Image | None:
    try:
        img = Image.open(path)
        img.load()
        return img
    except Exception as e:
        log.warning("decode_failed", path=str(path), error=str(e))
        return None


def detect_faces_all(
    store: Store,
    detector: FaceDetector,
    *,
    force: bool = False,
    decode_workers: int = 4,
    limit: int | None = None,
) -> dict[str, int]:
    """Walk the DB, detect faces for each image that doesn't have any yet."""
    counts = {"images": 0, "skipped": 0, "processed": 0, "failed": 0, "faces": 0}
    rows = list(store.iter_images())
    counts["images"] = len(rows)
    if limit is not None:
        rows = rows[:limit]

    todo = []
    for r in rows:
        if not force and store.has_faces(r.id, detector.name):
            counts["skipped"] += 1
            continue
        todo.append(r)
    log.info("faces_detect_started", n=len(todo), model=detector.name)

    decoded_q: queue.Queue[tuple[int, str, Image.Image | None] | object] = queue.Queue(maxsize=4)
    SENTINEL = object()

    def _decode(args: tuple[int, str]) -> tuple[int, str, Image.Image | None]:
        image_id, path = args
        return image_id, path, _open_image(Path(path))

    def producer() -> None:
        try:
            with ThreadPoolExecutor(max_workers=decode_workers) as ex:
                for r in todo:
                    decoded_q.put(ex.submit(_decode, (r.id, r.path)).result())
        finally:
            decoded_q.put(SENTINEL)

    threading.Thread(target=producer, daemon=True).start()

    while True:
        item = decoded_q.get()
        if item is SENTINEL:
            break
        assert isinstance(item, tuple)
        image_id, path, img = item
        if img is None:
            counts["failed"] += 1
            continue
        try:
            faces = detector.detect(img)
        except Exception as e:
            log.error("face_detect_failed", path=path, error=str(e))
            counts["failed"] += 1
            continue
        if force:
            with store.transaction():
                store.delete_faces_for_image(image_id, detector.name)
        with store.transaction():
            for face in faces:
                store.insert_face(
                    image_id=image_id,
                    bbox=face.bbox,
                    det_score=face.det_score,
                    embedding=face.embedding,
                    model_name=detector.name,
                    landmarks=face.landmarks,
                )
                counts["faces"] += 1
        counts["processed"] += 1

    log.info("faces_detect_done", **counts)
    return counts


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def cluster_faces(
    store: Store,
    *,
    min_cluster_size: int = 3,
    min_samples: int = 2,
    identity_match_threshold: float = 0.5,
    random_state: int = 42,
) -> int:
    """Cluster all face embeddings, persist run, carry forward identity names."""
    import hdbscan
    import umap

    face_ids, vectors = store.load_face_embeddings(MODEL_NAME)
    if vectors.shape[0] < min_cluster_size:
        raise ValueError(f"Not enough faces ({vectors.shape[0]}) for min_cluster_size={min_cluster_size}")
    log.info("faces_cluster_started", n=int(vectors.shape[0]), dim=int(vectors.shape[1]))

    n_components = min(50, max(2, vectors.shape[0] - 2))
    n_neighbors = min(30, max(2, vectors.shape[0] - 1))
    reduced = umap.UMAP(
        n_components=n_components,
        n_neighbors=n_neighbors,
        min_dist=0.0,
        metric="cosine",
        random_state=random_state,
    ).fit_transform(vectors)

    labels = hdbscan.HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",
        cluster_selection_method="eom",
    ).fit_predict(reduced)

    members: dict[int, list[tuple[int, np.ndarray, np.ndarray]]] = {}
    for fid, lab, vec, rvec in zip(face_ids, labels, vectors, reduced, strict=True):
        members.setdefault(int(lab), []).append((fid, vec, rvec))

    # Centroids in *embedding* space (used for identity matching) and in *reduced*
    # space (used for distance-to-centroid display).
    emb_centroids: dict[int, np.ndarray] = {}
    red_centroids: dict[int, np.ndarray] = {}
    for lv, m in members.items():
        if lv == -1:
            continue
        emb_centroids[lv] = np.vstack([v for _, v, _ in m]).mean(axis=0)
        red_centroids[lv] = np.vstack([rv for _, _, rv in m]).mean(axis=0)

    identities = store.list_face_identities()

    def match_identity(centroid: np.ndarray) -> dict[str, Any] | None:
        if not identities:
            return None
        best, best_sim = None, -1.0
        for ident in identities:
            sim = _cosine_sim(centroid, ident["centroid"])
            if sim > best_sim:
                best, best_sim = ident, sim
        if best is None or best_sim < identity_match_threshold:
            return None
        return best

    params = {
        "model": MODEL_NAME,
        "n_faces": int(vectors.shape[0]),
        "umap": {
            "n_components": n_components,
            "n_neighbors": n_neighbors,
            "metric": "cosine",
            "random_state": random_state,
        },
        "hdbscan": {
            "min_cluster_size": min_cluster_size,
            "min_samples": min_samples,
            "metric": "euclidean",
        },
        "identity_match_threshold": identity_match_threshold,
    }

    with store.transaction():
        run_id = store.create_face_run(params, _now_iso())
        for lv in sorted(members.keys()):
            m = members[lv]
            label_user = None
            label_auto = "noise" if lv == -1 else f"person {lv}"
            if lv != -1:
                hit = match_identity(emb_centroids[lv])
                if hit is not None:
                    label_user = hit["name"]
            cid = store.add_face_cluster(
                run_id=run_id,
                cluster_no=lv,
                size=len(m),
                label_auto=label_auto,
                label_user=label_user,
            )
            if lv == -1:
                for fid, _, _ in m:
                    store.assign_face_to_cluster(fid, cid, distance=0.0)
            else:
                centroid = red_centroids[lv]
                for fid, _, rvec in m:
                    d = float(np.linalg.norm(rvec - centroid))
                    store.assign_face_to_cluster(fid, cid, distance=d)
            # When a cluster carries a name, refresh the identity centroid with
            # the new evidence (running mean weighted by sample count).
            if lv != -1 and label_user is not None:
                store.upsert_face_identity(label_user, emb_centroids[lv], n_samples=len(m))

    log.info(
        "faces_cluster_done",
        run_id=run_id,
        n_clusters=sum(1 for k in members if k != -1),
        n_noise=len(members.get(-1, [])),
        named=sum(1 for lv in members if lv != -1 and match_identity(emb_centroids[lv])),
    )
    return run_id


def name_cluster(store: Store, cluster_id: int, name: str | None) -> None:
    """Set/clear `label_user` and update `face_identities`."""
    cluster = store.get_face_cluster(cluster_id)
    if cluster is None:
        raise ValueError(f"face cluster {cluster_id} not found")
    with store.transaction():
        store.set_face_cluster_label_user(cluster_id, name)
        if name:
            members = store.face_cluster_members(cluster_id)
            face_ids = [m["face_id"] for m in members]
            if face_ids:
                placeholders = ",".join("?" * len(face_ids))
                cur = store.conn.execute(
                    f"SELECT id, dim, embedding FROM faces WHERE id IN ({placeholders})",
                    face_ids,
                )
                vecs = [np.frombuffer(r["embedding"], dtype=np.float32, count=int(r["dim"])) for r in cur]
                if vecs:
                    centroid = np.mean(np.vstack(vecs), axis=0).astype(np.float32)
                    store.upsert_face_identity(name, centroid, n_samples=len(vecs))


def verify_faces(
    store: Store,
    *,
    min_det_score: float = 0.65,
    min_area: int = 32 * 32,
    apply: bool = False,
) -> dict[str, int]:
    """Walk faces, mark each as verified=1/0 based on cheap heuristics.

    With `apply=True`, faces failing verification are deleted (cluster sizes
    auto-adjusted via Store.delete_face). Otherwise the rows stay and the UI
    shows them with a low-confidence indicator (verified=0).

    Heuristics:
    - det_score < min_det_score → suspicious
    - bbox area < min_area      → too small for ArcFace to embed reliably
    """
    counts = {
        "checked": 0,
        "kept": 0,
        "low_score": 0,
        "small": 0,
        "deleted": 0,
        "skipped": 0,
    }
    rows = list(store.iter_faces_for_verify())
    log.info(
        "faces_verify_started",
        n=len(rows),
        min_det_score=min_det_score,
        min_area=min_area,
        apply=apply,
    )
    with store.transaction():
        for r in rows:
            counts["checked"] += 1
            if r["bbox"] is None:
                counts["skipped"] += 1
                continue
            area = int(r["bbox"][2]) * int(r["bbox"][3])
            score = float(r["det_score"])
            bad_score = score < min_det_score
            bad_size = area < min_area
            if bad_score or bad_size:
                if bad_score:
                    counts["low_score"] += 1
                if bad_size:
                    counts["small"] += 1
                if apply:
                    store.delete_face(int(r["id"]))
                    counts["deleted"] += 1
                else:
                    store.set_face_verified(int(r["id"]), 0)
            else:
                counts["kept"] += 1
                store.set_face_verified(int(r["id"]), 1)
    log.info("faces_verify_done", **counts)
    return counts


def crop_face(img: Image.Image, bbox: list[int], *, margin: float = 0.25) -> Image.Image:
    """Crop a face from an image with a margin, EXIF-corrected first.

    Bboxes are stored in the detector's coord space (max side `DETECT_MAX_SIDE`).
    The source image is resized to the same space before cropping so the
    coordinates line up regardless of original resolution.
    """
    src = ImageOps.exif_transpose(img).convert("RGB")
    src.thumbnail((DETECT_MAX_SIDE, DETECT_MAX_SIDE))
    x, y, w, h = bbox
    mx, my = int(w * margin), int(h * margin)
    left = max(0, x - mx)
    top = max(0, y - my)
    right = min(src.width, x + w + mx)
    bottom = min(src.height, y + h + my)
    return src.crop((left, top, right, bottom))


def cluster_color(cluster_id: int | None) -> str:
    """Stable hue per cluster id, returned as `hsl(...)`."""
    if cluster_id is None:
        return "hsl(0, 0%, 70%)"  # grey for unclustered
    h = (cluster_id * 137) % 360  # golden-angle spread
    return f"hsl({h}, 70%, 55%)"


__all__ = [
    "MODEL_NAME",
    "EMBED_DIM",
    "DetectedFace",
    "FaceDetector",
    "detect_faces_all",
    "cluster_faces",
    "name_cluster",
    "verify_faces",
    "crop_face",
    "cluster_color",
]
