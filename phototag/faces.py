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
# Cap the n_samples weight when blending an identity's centroid with new
# evidence. Without a cap, an identity that has accumulated thousands of
# samples becomes immutable — a one-photo recluster can never shift it
# back, and the person ageing/changing slowly drifts the *true* mean away
# from the stored centroid. 200 keeps long-term identities stable while
# allowing slow drift to follow the person.
IDENTITY_SAMPLE_CAP = 200
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
    """Decode an image and detach it from the underlying file handle.

    `Image.open` keeps the file open until `.close()`; under a 10k+ scan that
    leaks file descriptors. We open inside a context, force decode via .copy(),
    and let the with-block close the source handle.
    """
    try:
        with Image.open(path) as img:
            img.load()
            return img.copy()
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
        # Single transaction so a reader never sees the image with zero faces
        # mid-replacement when running with --force.
        with store.transaction():
            if force:
                store.delete_faces_for_image(image_id, detector.name)
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


def _hungarian_identity_match(
    cluster_ids: list[int],
    cluster_centroids: dict[int, np.ndarray],
    identities: list[dict[str, Any]],
    *,
    threshold: float,
) -> dict[int, dict[str, Any]]:
    """Assign at most one identity per new cluster (and vice versa) using the
    Hungarian algorithm so two new clusters can never claim the same identity.

    Returns {cluster_no: identity_row} for the matched pairs (sim >= threshold).
    Falls back to greedy if scipy isn't available — same correctness for the
    one-cluster-per-identity invariant, just less optimal under ties.
    """
    if not cluster_ids or not identities:
        return {}
    sim = np.zeros((len(cluster_ids), len(identities)), dtype=np.float32)
    for i, cid in enumerate(cluster_ids):
        c = cluster_centroids[cid]
        for j, ident in enumerate(identities):
            sim[i, j] = _cosine_sim(c, ident["centroid"])

    try:
        from scipy.optimize import linear_sum_assignment

        # Maximize similarity → solve as cost = -sim.
        rows, cols = linear_sum_assignment(-sim)
    except Exception:  # scipy missing or solver bailed; fall back to greedy.
        rows, cols = [], []
        used_ident: set[int] = set()
        order = sorted(
            range(len(cluster_ids)),
            key=lambda i: -float(sim[i].max() if sim.shape[1] else 0.0),
        )
        for i in order:
            j = int(np.argmax([sim[i, k] if k not in used_ident else -1.0 for k in range(len(identities))]))
            if sim[i, j] >= threshold and j not in used_ident:
                rows.append(i)
                cols.append(j)
                used_ident.add(j)

    out: dict[int, dict[str, Any]] = {}
    for i, j in zip(rows, cols, strict=True):
        if sim[i, j] >= threshold:
            out[cluster_ids[i]] = identities[j]
    return out


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
    identity_assignment = _hungarian_identity_match(
        sorted(emb_centroids.keys()),
        emb_centroids,
        identities,
        threshold=identity_match_threshold,
    )

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
                hit = identity_assignment.get(lv)
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
            # When a cluster carries a name, refresh the identity centroid as
            # a sample-weighted running mean (so a one-photo recluster doesn't
            # erase a hundred-photo identity).
            if lv != -1 and label_user is not None:
                hit = identity_assignment[lv]
                # Cap the prior weight so a long-lived identity can still
                # drift on new evidence (see IDENTITY_SAMPLE_CAP).
                n_old = min(int(hit.get("n_samples", 0)) or 0, IDENTITY_SAMPLE_CAP)
                old_c = np.asarray(hit["centroid"], dtype=np.float32)
                blended = ((old_c * n_old + emb_centroids[lv] * len(m)) / max(1, n_old + len(m))).astype(
                    np.float32, copy=False
                )
                # Display counter keeps the true sample count.
                true_n = (int(hit.get("n_samples", 0)) or 0) + len(m)
                store.upsert_face_identity(label_user, blended, n_samples=true_n)

    # Tier-1 sticky-label post-pass: replay user corrections so a 'named'
    # action you took last week survives this re-cluster, and an 'unassigned'
    # face doesn't slip back into the same wrong identity. Failure here must
    # not invalidate the cluster run itself — log loudly and move on.
    sticky: dict[str, int] = {"named": 0, "unassigned": 0}
    try:
        with store.transaction():
            sticky = apply_sticky_corrections(store, run_id)
    except Exception as e:
        log.error("sticky_pass_failed", run_id=run_id, error=str(e))

    log.info(
        "faces_cluster_done",
        run_id=run_id,
        n_clusters=sum(1 for k in members if k != -1),
        n_noise=len(members.get(-1, [])),
        named=len(identity_assignment),
        sticky=sticky,
    )
    return run_id


def cluster_orphan_faces(
    store: Store,
    *,
    min_cluster_size: int = 3,
    min_samples: int = 2,
    identity_match_threshold: float = 0.5,
    random_state: int = 42,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Re-cluster only orphan/noise faces (those not in any named cluster).

    Useful after the user has qualified a chunk of faces by hand: the now-
    smaller orphan pool can be re-clustered with looser parameters and any
    cluster whose centroid matches an existing identity reattaches the
    person's name automatically.

    `dry_run=True` returns the proposed clustering without writing anything.
    Otherwise a new face_run is created with `params_json.type = "orphan_refinement"`.
    Existing named clusters are NOT touched in either mode.
    """
    import hdbscan
    import umap

    face_ids, vectors = store.load_orphan_face_embeddings(MODEL_NAME)
    if vectors.shape[0] < min_cluster_size:
        return {
            "run_id": None,
            "dry_run": dry_run,
            "n_orphan": int(vectors.shape[0]),
            "n_clusters": 0,
            "n_noise": int(vectors.shape[0]),
            "named_via_identity": 0,
            "clusters": [],
            "error": (
                f"not enough orphan faces ({vectors.shape[0]}) for min_cluster_size={min_cluster_size}"
            ),
        }
    log.info(
        "faces_orphan_recluster_started",
        n=int(vectors.shape[0]),
        dim=int(vectors.shape[1]),
        dry_run=dry_run,
    )

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

    emb_centroids: dict[int, np.ndarray] = {}
    red_centroids: dict[int, np.ndarray] = {}
    for lv, m in members.items():
        if lv == -1:
            continue
        emb_centroids[lv] = np.vstack([v for _, v, _ in m]).mean(axis=0)
        red_centroids[lv] = np.vstack([rv for _, _, rv in m]).mean(axis=0)

    identities = store.list_face_identities()
    identity_assignment = _hungarian_identity_match(
        sorted(emb_centroids.keys()),
        emb_centroids,
        identities,
        threshold=identity_match_threshold,
    )

    summary_clusters: list[dict[str, Any]] = []
    for lv in sorted(k for k in members if k != -1):
        m = members[lv]
        hit = identity_assignment.get(lv)
        summary_clusters.append(
            {
                "cluster_no": int(lv),
                "size": len(m),
                "label_auto": f"orphan-cluster {lv}",
                "label_user": hit["name"] if hit else None,
                "sample_face_ids": [fid for fid, _, _ in m[:5]],
            }
        )

    n_clusters = len(summary_clusters)
    n_noise = len(members.get(-1, []))
    named_via_identity = sum(1 for c in summary_clusters if c["label_user"])

    if dry_run:
        log.info(
            "faces_orphan_recluster_dryrun",
            n_clusters=n_clusters,
            n_noise=n_noise,
            named_via_identity=named_via_identity,
        )
        return {
            "run_id": None,
            "dry_run": True,
            "n_orphan": int(vectors.shape[0]),
            "n_clusters": n_clusters,
            "n_noise": n_noise,
            "named_via_identity": named_via_identity,
            "clusters": summary_clusters,
        }

    params = {
        "type": "orphan_refinement",
        "model": MODEL_NAME,
        "n_orphan": int(vectors.shape[0]),
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
        # Detach the orphan faces from their previous (un-named) cluster
        # assignments first. Without this, the old noise/auto-cluster `size`
        # counters keep counting these faces forever and the unidentified
        # workspace would still list them via `list_faces_for_image` picking
        # the older run when the new run is the same numeric run_id.
        for fid in face_ids:
            store.unassign_face_globally(fid)

        run_id = store.create_face_run(params, _now_iso())
        for lv in sorted(members.keys()):
            m = members[lv]
            label_user = None
            label_auto = "noise" if lv == -1 else f"orphan-cluster {lv}"
            hit = identity_assignment.get(lv) if lv != -1 else None
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
            if hit is not None and label_user is not None:
                # Sample-weighted blend (same as cluster_faces).
                # Cap the prior weight so a long-lived identity can still
                # drift on new evidence (see IDENTITY_SAMPLE_CAP).
                n_old = min(int(hit.get("n_samples", 0)) or 0, IDENTITY_SAMPLE_CAP)
                old_c = np.asarray(hit["centroid"], dtype=np.float32)
                blended = ((old_c * n_old + emb_centroids[lv] * len(m)) / max(1, n_old + len(m))).astype(
                    np.float32, copy=False
                )
                # Display counter keeps the true sample count.
                true_n = (int(hit.get("n_samples", 0)) or 0) + len(m)
                store.upsert_face_identity(label_user, blended, n_samples=true_n)

    # Tier-1 sticky-label post-pass on the orphan run too.
    sticky: dict[str, int] = {"named": 0, "unassigned": 0}
    try:
        with store.transaction():
            sticky = apply_sticky_corrections(store, run_id)
    except Exception as e:
        log.error("sticky_pass_failed", run_id=run_id, error=str(e))

    log.info(
        "faces_orphan_recluster_done",
        run_id=run_id,
        n_clusters=n_clusters,
        n_noise=n_noise,
        named_via_identity=named_via_identity,
        sticky=sticky,
    )
    return {
        "run_id": run_id,
        "dry_run": False,
        "n_orphan": int(vectors.shape[0]),
        "n_clusters": n_clusters,
        "n_noise": n_noise,
        "named_via_identity": named_via_identity,
        "clusters": summary_clusters,
    }


def apply_sticky_corrections(store: Store, run_id: int) -> dict[str, int]:
    """Replay user corrections onto the freshly-created face_run.

    Tier-1 implementation (per spec/15-faces.md):
    - `named` corrections force the face's cluster to carry that label_user.
      If the cluster currently has a *different* label_user, the user's last
      naming wins.
    - `unassigned` corrections look up the face's current cluster in this run
      and, if its label_user matches the *old* cluster's identity (i.e. the
      auto-clusterer placed it there again), reassign the face to noise so
      the user has a chance to re-label it.
    Counts what it changed so callers can log it.
    """
    counts = {"named": 0, "unassigned": 0}
    # Resolve a single noise cluster id for this run, if any. We need it to
    # park `unassigned` corrections that re-fired.
    noise_row = store.conn.execute(
        "SELECT id FROM face_clusters WHERE run_id=? AND cluster_no=-1 LIMIT 1",
        (run_id,),
    ).fetchone()
    noise_cid = int(noise_row["id"]) if noise_row else None

    # SQL filter to the actions this pass actually consumes — `verified` /
    # `unverified` / `deleted` / `merged` rows would otherwise mask older
    # `named` / `unassigned` rows under the per-face dedup, and the table
    # grows monotonically so a long-lived corpus would scan O(N) audit rows
    # on every cluster_faces call.
    corrections = store.list_face_corrections(actions=["named", "unassigned"])
    # Most-recent-first (list_face_corrections returns ORDER BY id DESC)
    # → keep the latest user intent per face.
    seen: set[int] = set()
    for c in corrections:
        fid = c.get("face_id")
        if fid is None or int(fid) in seen:
            continue
        action = c["action"]
        # Map face_id → its cluster in *this* run (window pick).
        row = store.conn.execute(
            """
            SELECT fc.id AS cid, fc.label_user
            FROM face_cluster_assignments fca
            JOIN face_clusters fc ON fc.id = fca.cluster_id
            WHERE fca.face_id = ? AND fc.run_id = ?
            LIMIT 1
            """,
            (int(fid), run_id),
        ).fetchone()
        if row is None:
            continue
        cid = int(row["cid"])
        cur_label = row["label_user"]
        applied = False
        if action == "named" and c.get("name"):
            if cur_label != c["name"]:
                store.set_face_cluster_label_user(cid, str(c["name"]))
                counts["named"] += 1
                applied = True
            else:
                applied = True  # already correct → still consumes the per-face slot
        elif action == "unassigned":
            if noise_cid is None:
                # No noise cluster in this run (HDBSCAN packed everyone) —
                # nowhere to park the un-assignment. Don't mark the face as
                # `seen` so a later pass with noise can replay this row.
                continue
            if cid != noise_cid:
                store.conn.execute(
                    "DELETE FROM face_cluster_assignments WHERE face_id=? AND cluster_id=?",
                    (int(fid), cid),
                )
                store.conn.execute("UPDATE face_clusters SET size = MAX(0, size - 1) WHERE id=?", (cid,))
                store.assign_face_to_cluster(int(fid), noise_cid, distance=0.0)
                store.conn.execute("UPDATE face_clusters SET size = size + 1 WHERE id=?", (noise_cid,))
                counts["unassigned"] += 1
            applied = True
        if applied:
            seen.add(int(fid))
    return counts


def _identity_matrix(identities: list[dict[str, Any]], dim: int) -> tuple[list[dict[str, Any]], np.ndarray]:
    """Stack identity centroids of a given `dim` into one (N, D) matrix.

    Returns (filtered_identities, matrix_or_empty). Identities with mismatched
    dim are dropped — same defensive posture as `attach_face_to_best_identity`.
    """
    same_dim = [i for i in identities if int(i.get("dim", 0)) == dim]
    if not same_dim:
        return [], np.zeros((0, dim), dtype=np.float32)
    M = np.vstack([np.asarray(i["centroid"], dtype=np.float32) for i in same_dim])
    return same_dim, M


def _normalize_rows(M: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(M, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return M / n


def attach_face_to_best_identity(
    store: Store,
    face_id: int,
    embedding: np.ndarray,
    *,
    image_id: int | None = None,
    threshold: float = 0.5,
    auto_verify_threshold: float = 0.7,
    min_margin: float = 0.05,
    manual_run_id: int | None = None,
) -> dict[str, Any] | None:
    """Auto-cluster a single fresh face into a known identity.

    Returns a small report dict (`{"name", "sim", "user_verified"}`) on a
    match, or None if no identity is close enough.

    Two thresholds:
      - `threshold` (default 0.5): minimum cosine similarity to attach at all.
      - `auto_verify_threshold` (default 0.7): minimum sim to *also* mark
        the face `user_verified=1`. Below this band the face attaches to the
        identity but stays in the "?" not-yet-validated state so the user
        sees it as a suggestion to confirm.

    `image_id` is written into the audit row directly (cleaner than
    rewriting NULL post-hoc).

    `manual_run_id` lets a caller looping over many faces hoist the manual-
    run lookup once; defaults to per-call lookup.

    Identities whose centroid `dim` doesn't match the face embedding are
    skipped silently (defensive: protects against legacy DB rows).
    """
    identities = store.list_face_identities()
    if not identities:
        return None
    emb = np.asarray(embedding, dtype=np.float32)
    edim = int(emb.shape[0])

    # Tier-2 sticky labels (hard-negative mining): identities the user has
    # explicitly rejected for *this* face via an `unassigned` correction.
    # Skipping them here stops the loop from re-suggesting the same wrong
    # name re-cluster after re-cluster.
    cannot = store.cannot_link_identities_for_face(face_id)

    # Track top-2 so we can refuse to attach when the choice is ambiguous
    # (top1 - top2 < min_margin). On a real corpus we observed faces with
    # two near-equal candidates (e.g. 0.581 vs 0.577) — a 0.004 margin
    # where deterministic tie-break is essentially random and "named-but-
    # wrong" is worse than "still-orphan, prompt me".
    best_name: str | None = None
    best_sim = -1.0
    second_sim = -1.0
    best_centroid: np.ndarray | None = None
    best_n = 0
    for ident in identities:
        if int(ident.get("dim", 0)) != edim:
            continue
        if str(ident["name"]) in cannot:
            continue
        sim = _cosine_sim(emb, ident["centroid"])
        if sim > best_sim:
            second_sim = best_sim
            best_sim = sim
            best_name = str(ident["name"])
            best_centroid = np.asarray(ident["centroid"], dtype=np.float32)
            best_n = int(ident.get("n_samples", 0)) or 0
        elif sim > second_sim:
            second_sim = sim
    if best_name is None or best_sim < threshold or best_centroid is None:
        return None
    if second_sim >= 0.0 and (best_sim - second_sim) < min_margin:
        # Two identities are within the noise band — bail.
        return None

    # Locate or create the manual face_run (same one the manual-name path uses).
    if manual_run_id is None:
        row = store.conn.execute(
            "SELECT id FROM face_runs WHERE json_extract(params_json,'$.manual') = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        manual_run_id = (
            int(row["id"])
            if row
            else store.create_face_run({"manual": True, "model": MODEL_NAME}, _now_iso())
        )

    # Locate or create the same-name cluster in this manual run.
    crow = store.conn.execute(
        "SELECT id, size FROM face_clusters WHERE run_id=? AND label_user=?",
        (manual_run_id, best_name),
    ).fetchone()
    if crow:
        cid = int(crow["id"])
        store.assign_face_to_cluster(face_id, cid, distance=float(1.0 - best_sim))
        store.conn.execute("UPDATE face_clusters SET size = size + 1 WHERE id = ?", (cid,))
    else:
        max_no = store.conn.execute(
            "SELECT IFNULL(MAX(cluster_no), -1) AS m FROM face_clusters WHERE run_id=?",
            (manual_run_id,),
        ).fetchone()
        cluster_no = int(max_no["m"]) + 1
        cid = store.add_face_cluster(
            run_id=manual_run_id,
            cluster_no=cluster_no,
            size=1,
            label_auto=best_name,
            label_user=best_name,
        )
        store.assign_face_to_cluster(face_id, cid, distance=float(1.0 - best_sim))

    # Sample-weighted centroid update — cap the prior weight so identities
    # keep drifting on fresh evidence (see IDENTITY_SAMPLE_CAP).
    n_eff = min(best_n, IDENTITY_SAMPLE_CAP)
    blended = ((best_centroid * n_eff + emb) / max(1, n_eff + 1)).astype(np.float32, copy=False)
    store.upsert_face_identity(best_name, blended, n_samples=best_n + 1)

    # Auto-validate only when the match is comfortably above threshold —
    # marginal matches (sim in [threshold, auto_verify_threshold)) attach to
    # the identity but stay un-validated so the user reviews them via the "?"
    # marker and the validate-named bulk action.
    auto_validated = best_sim >= auto_verify_threshold
    if auto_validated:
        store.set_face_user_verified(face_id, 1)
    store.log_face_correction(
        face_id=face_id,
        image_id=image_id,
        action="named",
        cluster_id=cid,
        name=best_name,
    )
    # Symmetric with the manual-name path (see api_face_name): once a face
    # is bound to a real identity, its prior noise-cluster membership is
    # stale data that would (a) inflate the noise cluster's `size` and
    # (b) keep this face listed under "unidentified" until the next full
    # re-cluster.
    store.detach_face_from_noise(face_id)
    return {"name": best_name, "sim": float(best_sim), "user_verified": auto_validated}


def auto_attach_orphans(
    store: Store,
    *,
    threshold: float = 0.5,
    auto_verify_threshold: float = 0.7,
    min_margin: float = 0.05,
    dry_run: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    """Bulk-apply identity matching to every orphan face in one vectorized pass.

    Same logic as `attach_face_to_best_identity` (cosine sim → manual cluster
    + sample-weighted centroid update + tier-2 auto-validate band) but operates
    on the entire orphan pool with one `(N_orphans, D) @ (D, N_idents)` matmul
    instead of N×M Python-level dot products. ~50× faster on a real corpus.

    Returns a per-identity histogram + lists of (face_id, sim) attached. Dry-
    run does no writes.
    """
    orphan_ids, orphan_mat = store.load_orphan_face_embeddings(MODEL_NAME)
    if not orphan_ids:
        return {
            "n_orphan": 0,
            "matched": 0,
            "auto_validated": 0,
            "by_identity": {},
            "below_threshold": 0,
            "ambiguous": 0,
            "cannot_link_skipped": 0,
            "dry_run": dry_run,
        }
    if limit is not None:
        orphan_ids = orphan_ids[:limit]
        orphan_mat = orphan_mat[:limit]

    identities = store.list_face_identities()
    if not identities:
        return {
            "n_orphan": int(len(orphan_ids)),
            "matched": 0,
            "auto_validated": 0,
            "by_identity": {},
            "below_threshold": int(len(orphan_ids)),
            "ambiguous": 0,
            "cannot_link_skipped": 0,
            "dry_run": dry_run,
        }

    dim = orphan_mat.shape[1]
    same_dim, ident_mat = _identity_matrix(identities, dim)
    if ident_mat.shape[0] == 0:
        return {
            "n_orphan": int(len(orphan_ids)),
            "matched": 0,
            "auto_validated": 0,
            "by_identity": {},
            "below_threshold": int(len(orphan_ids)),
            "ambiguous": 0,
            "cannot_link_skipped": 0,
            "dry_run": dry_run,
        }

    # Vectorized cosine: normalize once, single matmul.
    on = _normalize_rows(orphan_mat)
    inn = _normalize_rows(ident_mat)
    sim = on @ inn.T  # (N_orphan, N_idents)

    # Tier-2 sticky labels (hard-negative mining): bulk-load every
    # cannot-link identity per orphan face from `face_corrections`, then
    # mask the forbidden (face, identity) pairs to -inf so they cannot win
    # the argmax and never count toward the top-2 margin check.
    cannot_by_face = store.cannot_link_identities_for_faces([int(f) for f in orphan_ids])
    cannot_link_skipped = 0
    if cannot_by_face:
        ident_names = [str(d["name"]) for d in same_dim]
        forbid = np.zeros(sim.shape, dtype=bool)
        for i, fid in enumerate(orphan_ids):
            blocked = cannot_by_face.get(int(fid))
            if not blocked:
                continue
            for j, name in enumerate(ident_names):
                if name in blocked:
                    forbid[i, j] = True
        cannot_link_skipped = int(forbid.sum())
        if cannot_link_skipped:
            sim = np.where(forbid, -np.inf, sim)

    best_j = np.argmax(sim, axis=1)
    best_s = sim[np.arange(sim.shape[0]), best_j]
    # Top-2 sim per row for the ambiguity check (set best to -inf, then
    # max again). When N_idents == 1, there is no second best — treat the
    # margin check as trivially satisfied.
    if sim.shape[1] >= 2:
        sim2 = sim.copy()
        sim2[np.arange(sim.shape[0]), best_j] = -np.inf
        second_s = sim2.max(axis=1)
    else:
        second_s = np.full_like(best_s, -np.inf)

    # Group attaches by identity so we can hoist the manual_run + per-name
    # cluster lookup once per identity instead of once per face. Faces with
    # an ambiguous top-2 (margin < min_margin) are dropped here so they
    # surface in the "below_threshold" tally for the report.
    matches: dict[int, list[tuple[int, float, np.ndarray]]] = {}
    below = 0
    ambiguous = 0
    for i, fid in enumerate(orphan_ids):
        s = float(best_s[i])
        if s < threshold:
            below += 1
            continue
        s2 = float(second_s[i])
        if s2 > -np.inf and (s - s2) < min_margin:
            ambiguous += 1
            continue
        matches.setdefault(int(best_j[i]), []).append((int(fid), s, orphan_mat[i]))

    by_identity: dict[str, dict[str, Any]] = {}
    matched = 0
    auto_validated = 0

    if dry_run:
        for j, items in matches.items():
            name = str(same_dim[j]["name"])
            n_validated = sum(1 for _, s, _ in items if s >= auto_verify_threshold)
            by_identity[name] = {
                "n": len(items),
                "auto_validated": n_validated,
                "min_sim": float(min(s for _, s, _ in items)),
                "max_sim": float(max(s for _, s, _ in items)),
            }
            matched += len(items)
            auto_validated += n_validated
        return {
            "n_orphan": int(len(orphan_ids)),
            "matched": matched,
            "auto_validated": auto_validated,
            "below_threshold": below,
            "ambiguous": ambiguous,
            "cannot_link_skipped": cannot_link_skipped,
            "by_identity": by_identity,
            "dry_run": True,
        }

    # Persist: hoist manual_run once, then per-identity reuse.
    with store.transaction():
        run_row = store.conn.execute(
            "SELECT id FROM face_runs WHERE json_extract(params_json,'$.manual') = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        manual_run_id = (
            int(run_row["id"])
            if run_row
            else store.create_face_run({"manual": True, "model": MODEL_NAME}, _now_iso())
        )
        # Bulk-load (face_id → image_id) so each attach call gets a real
        # image_id in its audit row instead of NULL. One SELECT vs N.
        all_orphan_ids = [fid for items in matches.values() for fid, _, _ in items]
        image_by_face: dict[int, int] = {}
        if all_orphan_ids:
            placeholders = ",".join("?" * len(all_orphan_ids))
            for r in store.conn.execute(
                f"SELECT id, image_id FROM faces WHERE id IN ({placeholders})",
                all_orphan_ids,
            ):
                image_by_face[int(r["id"])] = int(r["image_id"])
        for j, items in matches.items():
            name = str(same_dim[j]["name"])
            n_validated = 0
            for fid, _s, emb in items:
                hit = attach_face_to_best_identity(
                    store,
                    fid,
                    emb,
                    image_id=image_by_face.get(fid),
                    threshold=threshold,
                    auto_verify_threshold=auto_verify_threshold,
                    manual_run_id=manual_run_id,
                )
                if hit is not None:
                    matched += 1
                    if hit["user_verified"]:
                        n_validated += 1
                        auto_validated += 1
            by_identity[name] = {
                "n": len(items),
                "auto_validated": n_validated,
                "min_sim": float(min(s for _, s, _ in items)),
                "max_sim": float(max(s for _, s, _ in items)),
            }

    log.info(
        "orphans_auto_attached",
        n_orphan=len(orphan_ids),
        matched=matched,
        auto_validated=auto_validated,
        below_threshold=below,
        ambiguous=ambiguous,
        cannot_link_skipped=cannot_link_skipped,
    )
    return {
        "n_orphan": int(len(orphan_ids)),
        "matched": matched,
        "auto_validated": auto_validated,
        "below_threshold": below,
        "ambiguous": ambiguous,
        "cannot_link_skipped": cannot_link_skipped,
        "by_identity": by_identity,
        "dry_run": False,
    }


def name_cluster(store: Store, cluster_id: int, name: str | None) -> None:
    """Set/clear `label_user` and update `face_identities`.

    Refuses to set a non-null name on the noise cluster (cluster_no=-1):
    noise is a heterogeneous bag of faces and labelling it whole would tag
    every unrelated face on the same name. Clearing (name=None) is allowed
    so users can fix a previously-mislabelled noise cluster.
    """
    cluster = store.get_face_cluster(cluster_id)
    if cluster is None:
        raise ValueError(f"face cluster {cluster_id} not found")
    if name and int(cluster.get("cluster_no", 0)) == -1:
        raise ValueError(
            "cannot name the noise cluster — it groups unrelated faces. "
            "Use the per-face name action (POST /api/faces/{id}/name) "
            "to create a manual cluster instead."
        )
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
    "cluster_orphan_faces",
    "apply_sticky_corrections",
    "attach_face_to_best_identity",
    "auto_attach_orphans",
    "name_cluster",
    "verify_faces",
    "crop_face",
    "cluster_color",
]
