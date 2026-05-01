"""FastAPI UI for browsing tags / clusters and renaming/refining.

Endpoints
---------
GET  /                               → SPA shell
GET  /api/runs                       → list cluster runs (latest first)
GET  /api/runs/{run_id}/clusters     → clusters for a run
GET  /api/clusters/{id}              → cluster detail (members + top tags)
POST /api/clusters/{id}/rename       → set label_user
GET  /api/tags?prefix=...&limit=...  → tag autocomplete
GET  /api/search?tag=a&tag=b&...     → images matching ALL tags
GET  /api/images/{id}                → image metadata + tags
GET  /thumb/{id}                     → JPEG thumbnail (lazy cache on disk)
GET  /raw/{id}                       → raw image file
GET  /healthz                        → liveness
"""

import os
import secrets
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime as _dt
from pathlib import Path
from typing import Annotated, Any

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps
from pydantic import BaseModel
from starlette.requests import Request

from .faces import cluster_color
from .logging import get_logger, setup_logging
from .settings import load as load_settings
from .store import Store

log = get_logger(__name__)
THUMB_SIZE = 320
PREVIEW_SIZE = 1280
FACE_THUMB_SIZE = 192
THUMB_CACHE = Path("data/thumbs-cache")
PREVIEW_CACHE = Path("data/previews-cache")
FACE_THUMB_CACHE = Path("data/face-thumbs-cache")


class RenameRequest(BaseModel):
    label_user: str | None


class FaceNameRequest(BaseModel):
    name: str | None


class FaceIdentityMergeRequest(BaseModel):
    survivor: str
    loser: str


class CategoryNameRequest(BaseModel):
    name: str


class TagBindRequest(BaseModel):
    tag: str


class ClusterBindRequest(BaseModel):
    cluster_id: int


class BboxRedrawRequest(BaseModel):
    # bbox in DETECT_MAX_SIDE-resized coords, matching the storage format used
    # by `faces.detect()` and the face overlay render math.
    bbox: list[int]


def _store(app: FastAPI) -> Store:
    s = getattr(app.state, "store", None)
    if s is None:
        raise HTTPException(status_code=503, detail="store not initialized")
    return s  # type: ignore[no-any-return]


def _atomic_write_jpeg(dst: Path, save_fn: Any) -> None:
    """Write a JPEG via tmp + os.replace so concurrent readers never see a
    partial file. `save_fn(path)` is called with the temp path."""
    from contextlib import suppress

    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        save_fn(tmp)
        os.replace(tmp, dst)
    except Exception:
        with suppress(FileNotFoundError):
            tmp.unlink()
        raise


def _resized(src: Path, dst: Path, max_side: int, *, quality: int = 82) -> bool:
    try:
        with Image.open(src) as img:
            # exif_transpose applies the EXIF Orientation tag (rotate/flip) so the
            # bytes match the visual orientation. Strip-on-save means we must bake
            # the rotation into the pixels here, not rely on EXIF later.
            rgb = ImageOps.exif_transpose(img).convert("RGB")
            rgb.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            _atomic_write_jpeg(dst, lambda p: rgb.save(p, format="JPEG", quality=quality))
        return True
    except Exception as e:
        log.warning("resize_failed", src=str(src), max_side=max_side, error=str(e))
        return False


def create_app(db_path: Path | None = None) -> FastAPI:
    settings = load_settings()
    setup_logging(log_level=settings.log_level, json_logs=settings.json_logs)
    db = db_path or settings.db_path
    api_token = settings.api_token or None
    api_token_file = settings.api_token_file or None
    THUMB_CACHE.mkdir(parents=True, exist_ok=True)
    PREVIEW_CACHE.mkdir(parents=True, exist_ok=True)
    FACE_THUMB_CACHE.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.store = Store(db)
        app.state.face_detector = None
        app.state.face_detector_lock = threading.Lock()
        log.info("ui_started", db=str(db))
        try:
            yield
        finally:
            s = getattr(app.state, "store", None)
            if s is not None:
                s.close()

    app = FastAPI(title="phototag UI", lifespan=lifespan)
    # The UI is same-origin (the page and the API are served by the same
    # uvicorn process), so the browser doesn't trigger CORS preflight at all
    # for normal use. The allow-list below only matters if a different
    # localhost-port tool ever calls the API cross-origin. Default ports for
    # the typical dev/prod patterns are listed; other ports require the user
    # to extend this list (or proxy through nginx). Allowing `*` would be a
    # real risk under --host 0.0.0.0 since any LAN tab could hit DELETE/POST.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:8000",
            "http://localhost:8000",
            "http://127.0.0.1:8090",
            "http://localhost:8090",
        ],
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )

    project_root = Path(__file__).resolve().parent.parent
    templates = Jinja2Templates(directory=str(project_root / "templates"))
    app.mount("/static", StaticFiles(directory=str(project_root / "static")), name="static")

    if api_token or api_token_file:
        # Lightweight shared-secret guard. Disabled by default (the local-
        # loopback case). When set, every request except the index page,
        # /healthz, /static/* and CORS preflights (OPTIONS) must carry the
        # token via either:
        #   - X-API-Token header (preferred for fetch/XHR)
        #   - ?token=<value> query string (so <img> loads work in the SPA)
        # The token is also injected into the index template so the JS can
        # read it and decorate fetch/asset URLs automatically.
        #
        # When `api_token_file` is set, the middleware re-reads the file on
        # every protected request — editing the file rotates the token
        # without a restart. Cost is one tiny os.read per request; the
        # alternative (mtime watch / inotify) is more code for no measurable
        # win on a single-user local app.
        import secrets as _secrets

        from starlette.middleware.base import BaseHTTPMiddleware

        _PUBLIC = ("/", "/healthz", "/favicon.ico")
        _static_token: str | None = api_token
        _token_file: Path | None = api_token_file
        _last_seen: dict[str, str | None] = {"value": None}

        def _current_token() -> tuple[str | None, str | None]:
            """Return (token, error). Error is non-None when misconfigured /
            unreadable; in that case the middleware MUST 503 instead of
            silently letting the request through."""
            if _token_file is not None:
                try:
                    raw = _token_file.read_text().strip()
                except OSError as e:
                    log.error("api_token_file_read_failed", path=str(_token_file), error=str(e))
                    return None, "auth misconfigured: token file unreadable"
                if not raw:
                    if _static_token:
                        # Empty file but a fallback static token exists — use it.
                        return _static_token, None
                    return None, "auth misconfigured: token file empty and no APP_API_TOKEN"
                prev = _last_seen["value"]
                if prev is not None and prev != raw:
                    log.info("api_token_rotated", source="file")
                _last_seen["value"] = raw
                return raw, None
            # Static-only path; api_token is guaranteed truthy here.
            return _static_token, None

        class _AuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next: Any) -> Any:
                p = request.url.path
                # Let CORS preflight reach CORSMiddleware (which is registered
                # as inner middleware here) without needing a token. Native
                # asset loads (img/css) and the SPA shell are also public.
                if request.method == "OPTIONS" or p in _PUBLIC or p.startswith("/static/"):
                    return await call_next(request)
                expected, err = _current_token()
                if err is not None or expected is None:
                    return Response(status_code=503, content=err or "auth misconfigured")
                got = request.headers.get("X-API-Token") or request.query_params.get("token", "") or ""
                # constant-time comparison so a timing oracle can't probe the
                # token byte-by-byte.
                if not _secrets.compare_digest(got, expected):
                    return Response(status_code=401, content="missing or wrong API token")
                return await call_next(request)

        app.add_middleware(_AuthMiddleware)
        log.info(
            "api_token_enabled",
            source="file" if api_token_file else "env",
            file=str(api_token_file) if api_token_file else None,
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        # `version` busts /static cache on every restart so a regenerated
        # ui.css/ui.js takes effect on the next reload. The page itself
        # must NOT be cached, or the browser keeps serving an old `?v=`
        # and never picks up the regenerated bundles.
        from time import time as _time

        return templates.TemplateResponse(
            request,
            "ui.html",
            {
                "title": "phototag",
                "version": int(_time()),
                "api_token": api_token or "",
            },
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/api/runs")
    def api_runs() -> list[dict[str, Any]]:
        return _store(app).list_cluster_runs()

    @app.get("/api/runs/{run_id}/clusters")
    def api_clusters(run_id: int) -> list[dict[str, Any]]:
        return _store(app).list_clusters(run_id)

    @app.get("/api/clusters/{cluster_id}")
    def api_cluster(
        cluster_id: int,
        limit: Annotated[int, Query(ge=1, le=500)] = 60,
    ) -> dict[str, Any]:
        s = _store(app)
        meta = s.get_cluster(cluster_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="cluster not found")
        members = [
            # image_clusters.distance is always UMAP-Euclidean (no manual
            # write path here, unlike face_cluster_assignments). Kind is
            # surfaced for symmetry with the people endpoints — see store v9.
            {"image_id": iid, "path": p, "distance": d, "distance_kind": "euclidean_umap"}
            for iid, p, d in s.cluster_members(cluster_id, limit=limit)
        ]
        top_tags = [{"name": n, "count": c} for n, c in s.cluster_top_tags(cluster_id, top=30)]
        return {**meta, "members": members, "top_tags": top_tags}

    @app.post("/api/clusters/{cluster_id}/rename")
    def api_rename(cluster_id: int, body: RenameRequest) -> dict[str, Any]:
        s = _store(app)
        if s.get_cluster(cluster_id) is None:
            raise HTTPException(status_code=404, detail="cluster not found")
        with s.transaction():
            s.set_cluster_label_user(cluster_id, body.label_user or None)
        log.info("cluster_renamed", cluster_id=cluster_id, label_user=body.label_user)
        return {"ok": True, "cluster_id": cluster_id, "label_user": body.label_user}

    @app.get("/api/tags")
    def api_tags(
        prefix: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> list[dict[str, Any]]:
        return [{"name": n, "count": c} for n, c in _store(app).list_tag_names(prefix=prefix, limit=limit)]

    @app.get("/api/search")
    def api_search(
        tag: Annotated[list[str] | None, Query()] = None,
        person: Annotated[list[str] | None, Query()] = None,
        min_score: Annotated[float, Query(ge=0.0, le=1.0)] = 0.0,
        limit: Annotated[int, Query(ge=1, le=500)] = 120,
        run_id: int | None = None,
    ) -> list[dict[str, Any]]:
        s = _store(app)
        if not tag and not person:
            return []
        rid = run_id if run_id is not None else s.latest_cluster_run()
        person_ids: set[int] | None = s.search_images_by_persons(person) if person else None
        if person_ids is not None and not person_ids:
            return []
        if tag:
            results = s.search_images_by_tags(tag, min_score=min_score, limit=limit, run_id=rid)
            if person_ids is not None:
                results = [r for r in results if r["id"] in person_ids]
            return results
        # person-only — synthesize results from the image rows.
        assert person_ids is not None
        if not person_ids:
            return []
        placeholders = ",".join("?" * len(person_ids))
        cur = s.conn.execute(
            f"""
            SELECT i.id, i.path, NULL AS score,
                   NULL AS cluster_id, NULL AS cluster_no, NULL AS label_auto,
                   NULL AS label_user, NULL AS cluster_size
            FROM images i
            WHERE i.id IN ({placeholders})
            ORDER BY i.id
            LIMIT ?
            """,
            [*person_ids, limit],
        )
        return [dict(r) for r in cur]

    @app.get("/api/people/names")
    def api_people_names(
        prefix: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 30,
    ) -> list[dict[str, Any]]:
        return _store(app).list_named_people(prefix=prefix, limit=limit)

    @app.get("/api/people/by-name/{name}/clusters")
    def api_people_by_name_clusters(name: str) -> list[dict[str, Any]]:
        return _store(app).list_clusters_by_label(name)

    @app.post("/api/people/by-name/{name}/rename")
    def api_people_by_name_rename(name: str, body: FaceNameRequest) -> dict[str, Any]:
        """Rename every cluster currently labelled `name` to `body.name`.

        face_identities is keyed by name: we move the row (or merge into the
        target identity if it exists). Pass an empty/null name to clear all.
        """
        s = _store(app)
        new = (body.name or "").strip() or None
        if new == name:
            return {"renamed": 0, "from": name, "to": new}
        with s.transaction():
            n = s.rename_clusters_by_label(name, new)
            old_id = next((i for i in s.list_face_identities() if i["name"] == name), None)
            if old_id is not None:
                if new:
                    new_id = next(
                        (i for i in s.list_face_identities() if i["name"] == new),
                        None,
                    )
                    if new_id is not None:
                        n_old = old_id["n_samples"]
                        n_new = new_id["n_samples"]
                        blended = (new_id["centroid"] * n_new + old_id["centroid"] * n_old) / max(
                            1, n_new + n_old
                        )
                        s.upsert_face_identity(
                            new,
                            blended.astype(np.float32, copy=False),
                            n_samples=n_new + n_old,
                        )
                    else:
                        s.upsert_face_identity(
                            new,
                            old_id["centroid"].astype(np.float32, copy=False),
                            n_samples=old_id["n_samples"],
                        )
                s.delete_face_identity(name)
        log.info("face_identity_rename_all", from_=name, to=new, clusters=n)
        return {"renamed": n, "from": name, "to": new}

    @app.delete("/api/images/{image_id}/faces")
    def api_delete_image_faces(image_id: int) -> dict[str, Any]:
        """Drop every face on a single image (e.g. crowd / unhelpful detections)."""
        s = _store(app)
        if s.get_image(image_id) is None:
            raise HTTPException(status_code=404, detail="image not found")
        with s.transaction():
            n = s.delete_all_faces_for_image(image_id)
        log.info("image_faces_deleted", image_id=image_id, deleted=n)
        return {"deleted": n, "image_id": image_id}

    @app.delete("/api/images/{image_id}/faces/unidentified")
    def api_delete_image_unidentified_faces(image_id: int) -> dict[str, Any]:
        """Drop only faces that have no user-assigned name on this image.

        Useful after detection: keeps named faces, removes the noisy
        "person N"/no-name detections.
        """
        s = _store(app)
        if s.get_image(image_id) is None:
            raise HTTPException(status_code=404, detail="image not found")
        with s.transaction():
            n = s.delete_unidentified_faces_for_image(image_id)
        log.info("image_unidentified_faces_deleted", image_id=image_id, deleted=n)
        return {"deleted": n, "image_id": image_id}

    @app.delete("/api/faces/unidentified")
    def api_delete_all_unidentified_faces(yes: bool = False) -> dict[str, Any]:
        """Library-wide: drop every face that has no user-assigned name.

        Requires `?yes=true` so a stray cURL or accidental request doesn't
        wipe thousands of rows. UI sends it explicitly after `confirm()`.
        """
        if not yes:
            raise HTTPException(
                status_code=400,
                detail="library-wide delete requires ?yes=true",
            )
        s = _store(app)
        with s.transaction():
            n = s.delete_all_unidentified_faces()
        log.info("all_unidentified_faces_deleted", deleted=n)
        return {"deleted": n}

    @app.get("/api/faces/unidentified/summary")
    def api_unidentified_summary() -> dict[str, int]:
        return {"unidentified": _store(app).count_unidentified_faces()}

    @app.get("/api/faces/unidentified/images")
    def api_unidentified_images(
        limit: Annotated[int, Query(ge=1, le=2000)] = 500,
    ) -> list[dict[str, Any]]:
        """Photos containing at least one unidentified face.

        face_count is the *unidentified* count for that photo, not the total."""
        return _store(app).list_images_with_unidentified_faces(limit=limit)

    @app.get("/api/faces/triage")
    def api_faces_triage(
        limit: Annotated[int, Query(ge=1, le=2000)] = 300,
    ) -> list[dict[str, Any]]:
        """Photos needing user attention: ≥1 unverified named face OR a
        duplicate name (same `label_user` on ≥2 faces of the photo).

        Mirrors the response shape of `/api/faces/images` plus
        `n_unverified`, `n_dups`, `score`. Sorted by `score DESC`."""
        return _store(app).list_triage_images(limit=limit)

    @app.post("/api/faces/auto-attach-orphans")
    def api_auto_attach_orphans(
        threshold: float = 0.5,
        auto_verify_threshold: float = 0.7,
        dry_run: bool = True,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Bulk-attach orphan faces to known identities. Defaults to dry-run."""
        from .faces import auto_attach_orphans

        result = auto_attach_orphans(
            _store(app),
            threshold=threshold,
            auto_verify_threshold=auto_verify_threshold,
            limit=limit,
            dry_run=dry_run,
        )
        log.info(
            "auto_attach_orphans",
            dry_run=dry_run,
            n_orphan=result["n_orphan"],
            matched=result["matched"],
            auto_validated=result["auto_validated"],
        )
        return result

    @app.post("/api/faces/recluster-orphan")
    def api_recluster_orphan(
        min_size: int = 3,
        min_samples: int = 2,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Re-cluster orphan/noise faces. Default dry_run=true so a curl
        call without flags can preview without writing. Pass `dry_run=false`
        to persist a new face_run."""
        from .faces import cluster_orphan_faces

        result = cluster_orphan_faces(
            _store(app),
            min_cluster_size=min_size,
            min_samples=min_samples,
            dry_run=dry_run,
        )
        log.info(
            "orphan_recluster",
            dry_run=dry_run,
            n_orphan=result.get("n_orphan"),
            n_clusters=result.get("n_clusters"),
            named=result.get("named_via_identity"),
        )
        return result

    @app.post("/api/faces/clear-noise-labels")
    def api_clear_noise_labels() -> dict[str, int]:
        """One-shot fix for the bug where users named the noise cluster (which
        groups unrelated faces) and propagated the label to dozens of faces."""
        s = _store(app)
        with s.transaction():
            n = s.clear_noise_cluster_labels()
        log.info("noise_labels_cleared", rows=n)
        return {"cleared": n}

    @app.post("/api/images/{image_id}/redetect-faces")
    def api_redetect_faces(image_id: int) -> dict[str, Any]:
        """Re-run face detection on a single image.

        Validated faces (`user_verified=1`) are preserved when a freshly-
        detected box overlaps their stored bbox (IoU >= 0.4 — same coord
        space, since both are stored at DETECT_MAX_SIDE). A validated face
        whose position is no longer detected is dropped (the user can re-
        validate the new candidate). Non-validated faces are always replaced.
        """
        from PIL import Image as _Image

        from .faces import MODEL_NAME, FaceDetector

        s = _store(app)
        meta = s.get_image(image_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="image not found")
        src = s.absolute_path(meta["path"])
        if not src.exists():
            raise HTTPException(status_code=404, detail="file missing on disk")
        # Lazy global detector, kept on app.state so we don't reload weights per call.
        # The lock prevents two concurrent requests from both loading ~200 MB of
        # weights into RAM (and possibly OOMing) on the first call.
        det = app.state.face_detector
        if det is None:
            with app.state.face_detector_lock:
                det = app.state.face_detector
                if det is None:
                    settings = load_settings()
                    det = FaceDetector(settings.models_dir, device=settings.device)
                    app.state.face_detector = det
        try:
            with _Image.open(src) as img:
                img.load()
                new_faces = det.detect(img)
        except Exception as e:
            log.error("face_redetect_failed", path=str(src), error=str(e))
            raise HTTPException(status_code=500, detail=f"detect failed: {e}") from e

        def _iou(a: list[int], b: list[int]) -> float:
            ax, ay, aw, ah = a
            bx, by, bw, bh = b
            ix1, iy1 = max(ax, bx), max(ay, by)
            ix2, iy2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
            iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
            inter = iw * ih
            if inter <= 0:
                return 0.0
            union = aw * ah + bw * bh - inter
            return inter / union if union > 0 else 0.0

        IOU_KEEP = 0.4
        validated = s.list_user_verified_faces_for_image(image_id)
        # Largest-area first: greedy assignment is robust enough when the
        # biggest face claims its match before close-together small faces
        # (kid on a parent's lap, group photo) compete.
        validated_sorted = sorted(
            (v for v in validated if v["bbox"] and len(v["bbox"]) == 4),
            key=lambda v: -(int(v["bbox"][2]) * int(v["bbox"][3])),
        )
        keep_validated: set[int] = set()
        consumed_new: set[int] = set()
        for v in validated_sorted:
            best, best_iou = -1, 0.0
            for j, nf in enumerate(new_faces):
                if j in consumed_new:
                    continue
                iou = _iou(v["bbox"], nf.bbox)
                if iou > best_iou:
                    best, best_iou = j, iou
            if best >= 0 and best_iou >= IOU_KEEP:
                keep_validated.add(int(v["id"]))
                consumed_new.add(best)

        from .faces import attach_face_to_best_identity

        attached: list[dict[str, Any]] = []
        with s.transaction():
            # Drop every non-validated face first.
            s.delete_non_verified_faces_for_image(image_id, MODEL_NAME)
            # Drop validated faces whose position is no longer detected.
            for v in validated:
                if int(v["id"]) not in keep_validated:
                    s.delete_face(int(v["id"]))
            # Hoist the manual face_run lookup so the inner loop doesn't
            # re-query it per face.
            run_row = s.conn.execute(
                "SELECT id FROM face_runs WHERE json_extract(params_json,'$.manual') = 1 "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            from .faces import MODEL_NAME as _M

            manual_run_id = (
                int(run_row["id"])
                if run_row
                else s.create_face_run(
                    {"manual": True, "model": _M}, _dt.now(UTC).isoformat(timespec="seconds")
                )
            )
            # Insert new detections that did NOT match a kept validated face,
            # then immediately try to auto-attach each to a known identity by
            # cosine sim against face_identities centroids — so a freshly-
            # detected face for a known person gets the name without waiting
            # for a full `phototag faces cluster` run. High-confidence matches
            # (sim >= 0.7) are auto-validated; marginal matches stay "?" so
            # the user reviews via the validate-named bulk action.
            inserted = 0
            for j, f in enumerate(new_faces):
                if j in consumed_new:
                    continue
                fid = s.insert_face(
                    image_id=image_id,
                    bbox=f.bbox,
                    det_score=f.det_score,
                    embedding=f.embedding,
                    model_name=MODEL_NAME,
                    landmarks=f.landmarks,
                )
                inserted += 1
                hit = attach_face_to_best_identity(
                    s, fid, f.embedding, image_id=image_id, manual_run_id=manual_run_id
                )
                if hit is not None:
                    attached.append(hit)
        log.info(
            "face_redetected",
            image_id=image_id,
            detected=len(new_faces),
            validated_kept=len(keep_validated),
            validated_dropped=len(validated) - len(keep_validated),
            inserted=inserted,
            auto_attached=len(attached),
        )
        return {
            "image_id": image_id,
            "detected": len(new_faces),
            "validated_kept": len(keep_validated),
            "validated_dropped": len(validated) - len(keep_validated),
            "inserted": inserted,
            "auto_attached": attached,
        }

    @app.post("/api/faces/{face_id}/verify")
    def api_verify_face(face_id: int) -> dict[str, Any]:
        """User-confirms this face is a correct detection.

        Drives the verified styling and protects the face when the user runs
        "drop other dups of {name} on this image".
        """
        s = _store(app)
        face = s.get_face(face_id)
        if face is None:
            raise HTTPException(status_code=404, detail="face not found")
        with s.transaction():
            s.set_face_user_verified(face_id, 1)
            s.log_face_correction(
                face_id=face_id,
                image_id=int(face["image_id"]),
                action="verified",
            )
        log.info("face_user_verified", face_id=face_id)
        return {"ok": True, "face_id": face_id, "user_verified": 1}

    @app.post("/api/faces/{face_id}/unverify")
    def api_unverify_face(face_id: int) -> dict[str, Any]:
        s = _store(app)
        face = s.get_face(face_id)
        if face is None:
            raise HTTPException(status_code=404, detail="face not found")
        with s.transaction():
            s.set_face_user_verified(face_id, None)
            s.log_face_correction(
                face_id=face_id,
                image_id=int(face["image_id"]),
                action="unverified",
            )
        return {"ok": True, "face_id": face_id, "user_verified": None}

    @app.post("/api/images/{image_id}/faces/validate-named")
    def api_validate_named(image_id: int) -> dict[str, Any]:
        """Bulk-mark every named-but-not-yet-validated face on this image
        as user_verified=1. Use case: photo with a few well-clustered
        people, user trusts all the auto-assignments at once."""
        s = _store(app)
        if s.get_image(image_id) is None:
            raise HTTPException(status_code=404, detail="image not found")
        with s.transaction():
            ids = s.validate_named_unvalidated_for_image(image_id)
            for fid in ids:
                s.log_face_correction(face_id=fid, image_id=image_id, action="verified")
        log.info("named_validated_bulk", image_id=image_id, validated=len(ids))
        return {"validated": len(ids), "image_id": image_id}

    @app.delete("/api/images/{image_id}/faces/dups-of/{label}")
    def api_drop_dups_on_image(image_id: int, label: str, keep_face_id: int) -> dict[str, Any]:
        """Drop faces on this image that carry `label` (label_user) except
        `keep_face_id` (the verified one). Use after the user verifies the
        real detection of a person to clean up the false-positive duplicates."""
        s = _store(app)
        if s.get_image(image_id) is None:
            raise HTTPException(status_code=404, detail="image not found")
        with s.transaction():
            n = s.delete_other_named_faces_on_image(image_id, label, keep_face_id)
        log.info("dups_dropped", image_id=image_id, label=label, kept=keep_face_id, deleted=n)
        return {"deleted": n, "kept": keep_face_id, "label": label}

    @app.delete("/api/faces/{face_id}")
    def api_delete_face(face_id: int) -> dict[str, Any]:
        """Drop a false-positive face row."""
        s = _store(app)
        face = s.get_face(face_id)
        if face is None:
            raise HTTPException(status_code=404, detail="face not found")
        with s.transaction():
            s.log_face_correction(face_id=face_id, image_id=int(face["image_id"]), action="deleted")
            s.delete_face(face_id)
        log.info("face_deleted", face_id=face_id)
        return {"deleted": face_id}

    @app.post("/api/faces/{face_id}/redraw-bbox")
    def api_redraw_bbox(face_id: int, body: BboxRedrawRequest) -> dict[str, Any]:
        """Replace a face's geometry + embedding by re-running insightface
        on a user-drawn crop. (#13 — drag-to-redraw bbox)

        The bbox arrives in DETECT_MAX_SIDE-resized coords (same coord space
        as the stored bbox and the lightbox overlay). We:
          1. Load the source image, EXIF-transpose, thumbnail to DETECT_MAX_SIDE
             so the user's bbox is in the right coord system.
          2. Crop the user's region (clamped to image bounds).
          3. Run the existing FaceDetector on the crop. RetinaFace re-aligns
             whatever face it finds; ArcFace produces a fresh embedding.
          4. Pick the highest-`det_score` face inside the crop. If none, 422
             so the JS can keep the old bbox visible and surface the message.
          5. Translate the local bbox back into image coords (offset by the
             crop origin), update the row, evict the cached face thumb.

        Cluster assignment stays untouched on purpose — the identity link
        survives a bbox tweak. If the new embedding ends up far from the
        centroid, the next attach / verify pass will catch it.
        """
        from PIL import Image as _Image
        from PIL import ImageOps as _ImageOps

        from .faces import DETECT_MAX_SIDE, FaceDetector

        s = _store(app)
        face = s.get_face(face_id)
        if face is None:
            raise HTTPException(status_code=404, detail="face not found")
        if not body.bbox or len(body.bbox) != 4:
            raise HTTPException(status_code=400, detail="bbox must be [x, y, w, h]")
        meta = s.get_image(int(face["image_id"]))
        if meta is None:
            raise HTTPException(status_code=404, detail="image not found")
        src_path = s.absolute_path(meta["path"])
        if not src_path.exists():
            raise HTTPException(status_code=404, detail="file missing on disk")

        # Lazy-load detector once per process (mirrors api_redetect_faces).
        det = app.state.face_detector
        if det is None:
            with app.state.face_detector_lock:
                det = app.state.face_detector
                if det is None:
                    settings = load_settings()
                    det = FaceDetector(settings.models_dir, device=settings.device)
                    app.state.face_detector = det

        try:
            with _Image.open(src_path) as raw:
                raw.load()
                resized = _ImageOps.exif_transpose(raw).convert("RGB")
        except Exception as e:
            log.error("face_redraw_open_failed", path=str(src_path), error=str(e))
            raise HTTPException(status_code=500, detail=f"open failed: {e}") from e
        # Match the same thumbnail step the detector applies internally so
        # the bbox we received (DETECT_MAX_SIDE-space) lines up with the
        # image we crop from.
        resized.thumbnail((DETECT_MAX_SIDE, DETECT_MAX_SIDE))

        x, y, w, h = (int(c) for c in body.bbox)
        # Clamp to image bounds. A drag near the edge can produce a bbox
        # that extends slightly past the rendered image; PIL would happily
        # crop blank pixels which gives RetinaFace nothing to work with.
        W, H = resized.size
        x = max(0, min(x, W - 1))
        y = max(0, min(y, H - 1))
        w = max(1, min(w, W - x))
        h = max(1, min(h, H - y))
        if w < 24 or h < 24:
            # 24 px is the practical floor for RetinaFace at this resolution;
            # below that detection collapses to noise and we'd return junk.
            raise HTTPException(status_code=422, detail="bbox too small (need ≥24×24)")

        # Pad the crop so RetinaFace has context — it expects faces to sit
        # inside a frame that includes some shoulders / hair / background.
        # A tight 25 % margin (the original #13 default) frequently produced
        # empty detections on near-edge or square crops. We try 50 % first
        # and double the margin up to 200 % if RetinaFace still finds
        # nothing. Larger margins risk picking up a NEIGHBORING face, which
        # the centered-in-user-box filter below guards against.
        def _crop_with_margin(margin_frac: float) -> tuple[int, int, int, int, Image.Image]:
            mx = int(round(w * margin_frac))
            my = int(round(h * margin_frac))
            cx0 = max(0, x - mx)
            cy0 = max(0, y - my)
            cx1 = min(W, x + w + mx)
            cy1 = min(H, y + h + my)
            return cx0, cy0, cx1, cy1, resized.crop((cx0, cy0, cx1, cy1))

        # User's drawn rect is the "intended" target — any face whose
        # CENTER falls inside it is what they meant; faces whose centers
        # land elsewhere in the padded crop are rejected so we don't pick
        # up a neighbor when the margin balloons.
        def _center_in_user_box(local_bbox: list[int], cx0: int, cy0: int) -> bool:
            lx_, ly_, lw_, lh_ = local_bbox
            cx = cx0 + lx_ + lw_ / 2
            cy = cy0 + ly_ + lh_ / 2
            return bool(x <= cx <= x + w and y <= cy <= y + h)

        crop_origin = (0, 0)
        best = None
        for margin_frac in (0.5, 1.0, 2.0):
            cx0, cy0, _cx1, _cy1, crop = _crop_with_margin(margin_frac)
            try:
                faces = det.detect(crop)
            except Exception as e:
                log.error(
                    "face_redraw_detect_failed",
                    face_id=face_id,
                    margin=margin_frac,
                    error=str(e),
                )
                raise HTTPException(status_code=500, detail=f"detect failed: {e}") from e
            # Prefer faces whose center sits inside the user's drawn rect;
            # within those, take the highest det_score. Falling back to all
            # detected faces (without the centered filter) is dangerous —
            # we'd happily lock onto whoever's standing next to the target.
            centered = [f for f in faces if _center_in_user_box(f.bbox, cx0, cy0)]
            if centered:
                centered.sort(key=lambda f: f.det_score, reverse=True)
                best = centered[0]
                crop_origin = (cx0, cy0)
                break
            log.info(
                "face_redraw_widening_crop",
                face_id=face_id,
                margin=margin_frac,
                n_faces_in_crop=len(faces),
            )
        if best is None:
            raise HTTPException(
                status_code=422,
                detail="no face found inside the redrawn box; widen it or pick a clearer crop",
            )
        cx0, cy0 = crop_origin
        lx, ly, lw, lh = best.bbox
        # Translate back to image (DETECT_MAX_SIDE) coords.
        new_bbox = [int(cx0 + lx), int(cy0 + ly), int(lw), int(lh)]
        # Translate landmarks too if present (insightface returns absolute
        # coords inside the crop).
        new_landmarks: list[list[float]] | None
        if best.landmarks:
            new_landmarks = [[float(cx0 + p[0]), float(cy0 + p[1])] for p in best.landmarks]
        else:
            new_landmarks = None

        with s.transaction():
            s.update_face_bbox_and_embedding(
                face_id,
                bbox=new_bbox,
                embedding=best.embedding,
                det_score=float(best.det_score),
                landmarks=new_landmarks,
            )

        # Evict the cached face thumb so the next /face-thumb/{id} re-renders
        # from the new bbox. Best-effort; thumb regeneration is idempotent.
        thumb = FACE_THUMB_CACHE / f"{face_id}.jpg"
        try:
            thumb.unlink(missing_ok=True)
        except Exception as e:
            log.warning("face_thumb_evict_failed", face_id=face_id, error=str(e))

        log.info(
            "face_bbox_redrawn",
            face_id=face_id,
            old_bbox=face["bbox"],
            new_bbox=new_bbox,
            det_score=float(best.det_score),
        )
        return {
            "face_id": face_id,
            "bbox": new_bbox,
            "det_score": float(best.det_score),
            # `verified=1` is forced by `update_face_bbox_and_embedding`;
            # surface it so the JS doesn't have to re-fetch the face row to
            # re-render without the suspect badge.
            "verified": 1,
        }

    @app.post("/api/faces/{face_id}/unassign")
    def api_unassign_face(face_id: int) -> dict[str, Any]:
        """Remove this face's cluster assignment in the most recent run that
        actually holds it.

        Use case: face was clustered to the wrong person. The face row stays
        so the next `phototag faces cluster` can re-place it. We log the
        old cluster_id in face_corrections so future passes can use it as a
        cannot-link constraint.

        Targeting the most-recent-run-holding-this-face (not the global latest
        run) means a face stuck in an old manual run still gets unassigned;
        after this call the face is orphan / unidentified until a new
        `phototag faces cluster` pass picks it up again. Faces that were only
        in the noise cluster therefore become true orphans here, which is the
        desired outcome — noise is not a real identity.
        """
        s = _store(app)
        face = s.get_face(face_id)
        if face is None:
            raise HTTPException(status_code=404, detail="face not found")
        with s.transaction():
            # Use the run that the user is *actually looking at* — i.e. the
            # most recent run holding this face. Falling back to the global
            # latest_face_run() can no-op when the displayed cluster is from a
            # manual run that predates a later auto run.
            target_run = s.latest_run_holding_face(face_id)
            if target_run is not None:
                scoped_clusters = s.face_clusters_for_face_in_run(face_id, target_run)
                removed = s.unassign_face_from_run(face_id, target_run) if scoped_clusters else 0
            else:
                scoped_clusters = s.face_clusters_for_face(face_id)
                removed = s.unassign_face_globally(face_id) if scoped_clusters else 0
            for cid in scoped_clusters:
                s.log_face_correction(
                    face_id=face_id,
                    image_id=int(face["image_id"]),
                    action="unassigned",
                    cluster_id=cid,
                )
        log.info("face_unassigned", face_id=face_id, removed=removed)
        return {"unassigned": face_id, "removed": removed}

    @app.post("/api/people/by-name/{name}/split")
    def api_people_by_name_split(name: str) -> dict[str, Any]:
        """Suffix each cluster currently labelled `name` as `name 1`, `name 2`, …

        Replaces the single shared identity with one per split (each cluster's
        own centroid). Re-group later by renaming them back to the same name.
        """
        s = _store(app)
        clusters = s.list_clusters_by_label(name)
        if not clusters:
            raise HTTPException(status_code=404, detail="no clusters with this name")
        with s.transaction():
            new_names: list[str] = []
            for i, c in enumerate(clusters, start=1):
                new_name = f"{name} {i}"
                s.set_face_cluster_label_user(int(c["id"]), new_name)
                centroid = s.cluster_centroid(int(c["id"]))
                if centroid is not None:
                    s.upsert_face_identity(new_name, centroid, n_samples=int(c["size"]))
                new_names.append(new_name)
            s.delete_face_identity(name)
        log.info("face_identity_split", name=name, into=new_names)
        return {"split": len(clusters), "from": name, "into": new_names}

    @app.post("/api/face-identities/merge")
    def api_face_identities_merge(body: FaceIdentityMergeRequest) -> dict[str, Any]:
        """Merge two `face_identities` rows representing the same person.

        Sample-weighted centroid blend (cap=200, mirrors
        `phototag.faces.IDENTITY_SAMPLE_CAP`), summed display `n_samples`,
        re-label every cluster of `loser` to `survivor`, drop the loser row.
        """
        survivor = (body.survivor or "").strip()
        loser = (body.loser or "").strip()
        if not survivor or not loser:
            raise HTTPException(status_code=400, detail="survivor and loser required")
        if survivor == loser:
            raise HTTPException(status_code=400, detail="survivor and loser must differ")
        s = _store(app)
        names = {i["name"] for i in s.list_face_identities()}
        if survivor not in names:
            raise HTTPException(status_code=404, detail=f"survivor identity not found: {survivor}")
        if loser not in names:
            raise HTTPException(status_code=404, detail=f"loser identity not found: {loser}")
        with s.transaction():
            result = s.merge_face_identities(survivor, loser)
        log.info(
            "face_identity_merge",
            survivor=survivor,
            loser=loser,
            renamed_clusters=result["renamed_clusters"],
            n_samples=result["n_samples"],
        )
        return result

    @app.get("/api/images/{image_id}")
    def api_image(image_id: int) -> dict[str, Any]:
        s = _store(app)
        meta = s.get_image(image_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="image not found")
        tags = [{"name": n, "score": sc} for n, sc in s.list_image_tags(image_id)]
        exif = s.get_image_exif(image_id)
        return {**meta, "tags": tags, "exif": exif}

    # Allow the browser to cache image content for a day, but require it to
    # revalidate (no `immutable`) — that way a code change that re-generates
    # thumbs/previews server-side actually shows up after a normal reload.
    _CACHE_HEADERS = {"Cache-Control": "public, max-age=86400, must-revalidate"}

    def _serve_resized(image_id: int, cache_dir: Path, max_side: int, quality: int) -> FileResponse:
        s = _store(app)
        meta = s.get_image(image_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="image not found")
        dst = cache_dir / f"{image_id}.jpg"
        if not dst.exists():
            src = s.absolute_path(meta["path"])
            if not src.exists() or not _resized(src, dst, max_side, quality=quality):
                raise HTTPException(status_code=404, detail="image not available")
        return FileResponse(dst, media_type="image/jpeg", headers=_CACHE_HEADERS)

    @app.get("/thumb/{image_id}")
    def thumb(image_id: int) -> FileResponse:
        return _serve_resized(image_id, THUMB_CACHE, THUMB_SIZE, 82)

    @app.get("/preview/{image_id}")
    def preview(image_id: int) -> FileResponse:
        return _serve_resized(image_id, PREVIEW_CACHE, PREVIEW_SIZE, 85)

    @app.get("/raw/{image_id}")
    def raw(image_id: int) -> FileResponse:
        s = _store(app)
        meta = s.get_image(image_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="image not found")
        src = s.absolute_path(meta["path"])
        if not src.exists():
            raise HTTPException(status_code=404, detail="file not found on disk")
        return FileResponse(src, headers=_CACHE_HEADERS)

    # ---- faces (v2) ----

    @app.get("/api/faces/summary")
    def api_faces_summary() -> dict[str, int]:
        return _store(app).faces_summary()

    @app.get("/api/faces/images")
    def api_faces_images(
        limit: Annotated[int, Query(ge=1, le=2000)] = 300,
    ) -> list[dict[str, Any]]:
        return _store(app).list_images_with_faces(limit=limit)

    @app.get("/api/images/{image_id}/faces")
    def api_image_faces(image_id: int) -> list[dict[str, Any]]:
        s = _store(app)
        if s.get_image(image_id) is None:
            raise HTTPException(status_code=404, detail="image not found")
        out = []
        seen: set[int] = set()
        # `list_faces_for_image` already picks the most-recent-per-face run
        # via a window function, so each face appears once.
        for f in s.list_faces_for_image(image_id):
            if f["id"] in seen:
                continue
            seen.add(f["id"])
            out.append(
                {
                    "id": f["id"],
                    "bbox": f["bbox"],
                    "det_score": f["det_score"],
                    "verified": f["verified"],
                    "user_verified": f.get("user_verified"),
                    "cluster_id": f["cluster_id"],
                    "cluster_no": f["cluster_no"],
                    "label": f["label_user"] or f["label_auto"],
                    "named": f["label_user"] is not None,
                    "color": cluster_color(f["cluster_id"]),
                    "attach_sim": f.get("attach_sim"),
                    "distance": f.get("distance"),
                    "distance_kind": f.get("distance_kind"),
                }
            )
        return out

    @app.get("/api/people")
    def api_people(
        run_id: int | None = None,
        only_named: bool = False,
        only_unnamed: bool = False,
    ) -> list[dict[str, Any]]:
        s = _store(app)
        rid = run_id if run_id is not None else s.latest_face_run()
        if rid is None:
            return []
        rows = s.list_face_clusters(rid)
        out = []
        for c in rows:
            if c["cluster_no"] == -1:
                continue
            if only_named and not c["label_user"]:
                continue
            if only_unnamed and c["label_user"]:
                continue
            members = s.face_cluster_members(c["id"], limit=3)
            out.append(
                {
                    "cluster_id": c["id"],
                    "cluster_no": c["cluster_no"],
                    "name": c["label_user"],
                    "auto": c["label_auto"],
                    "size": c["size"],
                    "color": cluster_color(c["id"]),
                    "samples": [{"face_id": m["face_id"], "image_id": m["image_id"]} for m in members],
                }
            )
        return out

    @app.get("/api/people/by-name/{name}")
    def api_person_by_name(
        name: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> dict[str, Any]:
        """Merged person view: every face_cluster sharing this label_user.

        Returns the underlying clusters (each with up to `limit` members),
        plus aggregate counts for the header.
        """
        s = _store(app)
        clusters = s.list_clusters_by_label(name)
        if not clusters:
            raise HTTPException(status_code=404, detail="no clusters with this name")
        groups: list[dict[str, Any]] = []
        seen_images: set[int] = set()
        for c in clusters:
            members = s.face_cluster_members(int(c["id"]), limit=limit)
            for m in members:
                seen_images.add(int(m["image_id"]))
            groups.append({**c, "color": cluster_color(int(c["id"])), "members": members})
        return {
            "name": name,
            "n_clusters": len(clusters),
            "n_photos": len(seen_images),
            "groups": groups,
        }

    @app.get("/api/people/by-name/{name}/edge")
    def api_person_edge(
        name: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 9,
    ) -> list[dict[str, Any]]:
        """Most-distant-from-centroid faces across every cluster of `name`.

        The "fringe" of an identity — members the clusterer is least sure
        about. Returned sorted DESC by distance so triage starts at the
        riskiest face.
        """
        s = _store(app)
        if not s.list_clusters_by_label(name):
            raise HTTPException(status_code=404, detail="no clusters with this name")
        return s.list_edge_faces_by_label(name, limit=limit)

    @app.get("/api/people/{cluster_id}")
    def api_person(
        cluster_id: int,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> dict[str, Any]:
        s = _store(app)
        cluster = s.get_face_cluster(cluster_id)
        if cluster is None:
            raise HTTPException(status_code=404, detail="face cluster not found")
        members = s.face_cluster_members(cluster_id, limit=limit)
        return {
            **cluster,
            "color": cluster_color(cluster_id),
            "members": members,
        }

    @app.post("/api/people/{cluster_id}/name")
    def api_person_rename(cluster_id: int, body: FaceNameRequest) -> dict[str, Any]:
        from .faces import name_cluster

        s = _store(app)
        if s.get_face_cluster(cluster_id) is None:
            raise HTTPException(status_code=404, detail="face cluster not found")
        try:
            name_cluster(s, cluster_id, body.name or None)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        log.info("face_cluster_named", cluster_id=cluster_id, name=body.name)
        return {"ok": True, "cluster_id": cluster_id, "name": body.name}

    @app.post("/api/faces/{face_id}/name")
    def api_face_name(face_id: int, body: FaceNameRequest) -> dict[str, Any]:
        """Name a face directly. Used when no clustering run exists yet.

        Creates a single 'manual' face_run shared across all hand-named clusters,
        groups same-name faces into one cluster, and updates the identity centroid
        as a running mean so the next `phototag faces cluster` carries the name
        forward to all visually similar faces.
        """
        s = _store(app)
        face = s.get_face(face_id)
        if face is None:
            raise HTTPException(status_code=404, detail="face not found")
        name = (body.name or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name required")

        from .faces import MODEL_NAME

        # Locate or create the manual face_run.
        row = s.conn.execute(
            "SELECT id FROM face_runs WHERE json_extract(params_json,'$.manual') = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            run_id = int(row["id"])
        else:
            run_id = s.create_face_run(
                {"manual": True, "model": MODEL_NAME},
                _dt.now(UTC).isoformat(timespec="seconds"),
            )

        # Group same-name faces into one cluster within the manual run.
        crow = s.conn.execute(
            "SELECT id, size FROM face_clusters WHERE run_id=? AND label_user=?",
            (run_id, name),
        ).fetchone()
        with s.transaction():
            # Manual name: distance=0.0 is a placeholder (the user asserted
            # the match), and the kind is recorded as 'cosine_dist' to keep
            # the manual run consistent with auto-attach (see store v9).
            if crow:
                cid = int(crow["id"])
                s.assign_face_to_cluster(face_id, cid, distance=0.0, distance_kind="cosine_dist")
                s.conn.execute("UPDATE face_clusters SET size=size+1 WHERE id=?", (cid,))
            else:
                max_no_row = s.conn.execute(
                    "SELECT IFNULL(MAX(cluster_no), -1) AS m FROM face_clusters WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                cluster_no = int(max_no_row["m"]) + 1
                cid = s.add_face_cluster(
                    run_id=run_id,
                    cluster_no=cluster_no,
                    size=1,
                    label_auto=name,
                    label_user=name,
                )
                s.assign_face_to_cluster(face_id, cid, distance=0.0, distance_kind="cosine_dist")

            # Running-mean identity centroid so future cluster runs match.
            emb_row = s.conn.execute("SELECT dim, embedding FROM faces WHERE id=?", (face_id,)).fetchone()
            emb = np.frombuffer(emb_row["embedding"], dtype=np.float32, count=int(emb_row["dim"]))
            existing = next((i for i in s.list_face_identities() if i["name"] == name), None)
            if existing:
                n0 = existing["n_samples"]
                blended = (existing["centroid"] * n0 + emb) / (n0 + 1)
                s.upsert_face_identity(name, blended.astype(np.float32, copy=False), n_samples=n0 + 1)
            else:
                s.upsert_face_identity(name, emb, n_samples=1)
            s.log_face_correction(
                face_id=face_id,
                image_id=int(face["image_id"]),
                action="named",
                cluster_id=cid,
                name=name,
            )
            # Naming a face is an explicit user assertion that this face is
            # this person → mark it user_verified at the same time so the
            # green check shows up immediately and dup-drop spares it.
            s.set_face_user_verified(face_id, 1)
            # Naming is the user telling us this face has a real identity;
            # the noise membership is now stale and would (a) inflate the
            # noise cluster's size and (b) keep this face listed under
            # "unidentified". Detach it.
            detached = s.detach_face_from_noise(face_id)

        log.info(
            "face_named_manual",
            face_id=face_id,
            cluster_id=cid,
            name=name,
            detached_from_noise=detached,
        )
        return {"ok": True, "face_id": face_id, "cluster_id": cid, "name": name}

    @app.get("/api/faces/{face_id}/suggest")
    def api_face_suggest(
        face_id: int,
        k: Annotated[int, Query(ge=1, le=20)] = 3,
    ) -> list[dict[str, Any]]:
        """Top-K identity suggestions for a face by cosine vs face_identities.

        Returns `[{name, sim, n_samples}, ...]` sorted by sim desc. No
        threshold filter — caller decides what to surface. Empty list when
        no identities exist or none share the embedding's `dim`.
        """
        from .faces import _identity_matrix, _normalize_rows

        s = _store(app)
        face = s.get_face(face_id)
        if face is None:
            raise HTTPException(status_code=404, detail="face not found")
        emb_row = s.conn.execute(
            "SELECT dim, embedding FROM faces WHERE id=?",
            (face_id,),
        ).fetchone()
        if emb_row is None or emb_row["embedding"] is None:
            return []
        dim = int(emb_row["dim"])
        emb = np.frombuffer(emb_row["embedding"], dtype=np.float32, count=dim)
        identities = s.list_face_identities()
        if not identities:
            return []
        # Honor tier-2 sticky labels: if the user has previously rejected
        # an identity for this face, don't suggest it back. Same filter as
        # `attach_face_to_best_identity` so the popover never offers a
        # match the system itself would refuse to attach.
        cannot = s.cannot_link_identities_for_face(face_id)
        if cannot:
            identities = [i for i in identities if str(i["name"]) not in cannot]
            if not identities:
                return []
        same_dim, M = _identity_matrix(identities, dim)
        if M.shape[0] == 0:
            return []
        # Vectorized cosine: normalize both sides, single matvec.
        v = emb / (np.linalg.norm(emb) or 1.0)
        Mn = _normalize_rows(M)
        sims = Mn @ v  # (N_idents,)
        order = np.argsort(-sims)[:k]
        return [
            {
                "name": str(same_dim[int(j)]["name"]),
                "sim": float(sims[int(j)]),
                "n_samples": int(same_dim[int(j)].get("n_samples", 0)) or 0,
            }
            for j in order
        ]

    @app.get("/api/faces/corrections")
    def api_face_corrections(
        action: str | None = None,
        face_id: int | None = None,
        limit: Annotated[int, Query(ge=1, le=2000)] = 200,
    ) -> list[dict[str, Any]]:
        """Audit log of user-driven corrections (named / unassigned / deleted)."""
        rows = _store(app).list_face_corrections(face_id=face_id, action=action)
        return rows[:limit]

    @app.get("/face-thumb/{face_id}")
    def face_thumb(face_id: int) -> FileResponse:
        s = _store(app)
        face = s.get_face(face_id)
        if face is None:
            raise HTTPException(status_code=404, detail="face not found")
        meta = s.get_image(int(face["image_id"]))
        if meta is None:
            raise HTTPException(status_code=404, detail="image not found")
        dst = FACE_THUMB_CACHE / f"{face_id}.jpg"
        if not dst.exists():
            try:
                from .faces import crop_face

                with Image.open(s.absolute_path(meta["path"])) as src_img:
                    cropped = crop_face(src_img, face["bbox"])
                    cropped.thumbnail((FACE_THUMB_SIZE, FACE_THUMB_SIZE))
                    # Atomic write — two concurrent face_thumb requests for
                    # the same id would otherwise race (one truncates while
                    # the other reads). Mirrors `_resized` for /thumb + /preview.
                    _atomic_write_jpeg(dst, lambda p: cropped.save(p, format="JPEG", quality=85))
            except Exception as e:
                log.warning("face_thumb_failed", face_id=face_id, error=str(e))
                raise HTTPException(status_code=500, detail="thumb failed") from e
        return FileResponse(dst, media_type="image/jpeg", headers=_CACHE_HEADERS)

    # ---- categories (#23 UI) -----------------------------------------------
    # Mirrors the `phototag category` CLI surface: list, add, remove, plus rule
    # bind/unbind for both tag→category and face_cluster→category. The xmp
    # writer reads the same rules to populate `lr:HierarchicalSubject`.

    @app.get("/api/categories")
    def api_categories_list() -> list[dict[str, Any]]:
        s = _store(app)
        cats = s.list_categories()
        # Cheap aggregate counts so the panel can show "(3 tag rules)" badges
        # without a per-row roundtrip.
        tag_rules = s.list_tag_category_rules()
        cluster_rules = s.list_cluster_category_rules()
        n_tag = {c["name"]: 0 for c in cats}
        n_cluster = {c["name"]: 0 for c in cats}
        for r in tag_rules:
            n_tag[r["category"]] = n_tag.get(r["category"], 0) + 1
        for r in cluster_rules:
            n_cluster[r["category"]] = n_cluster.get(r["category"], 0) + 1
        return [
            {
                "id": int(c["id"]),
                "name": c["name"],
                "n_tag_rules": n_tag.get(c["name"], 0),
                "n_cluster_rules": n_cluster.get(c["name"], 0),
            }
            for c in cats
        ]

    @app.post("/api/categories", status_code=201)
    def api_categories_add(body: CategoryNameRequest) -> dict[str, Any]:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="name required")
        s = _store(app)
        with s.transaction():
            cid = s.add_category(name)
        return {"id": cid, "name": name}

    @app.delete("/api/categories/{name}")
    def api_categories_delete(name: str) -> dict[str, Any]:
        s = _store(app)
        with s.transaction():
            n = s.delete_category(name)
        if n == 0:
            raise HTTPException(status_code=404, detail="category not found")
        return {"deleted": n, "name": name}

    @app.get("/api/categories/{name}")
    def api_categories_detail(name: str) -> dict[str, Any]:
        s = _store(app)
        cat = s.get_category_by_name(name)
        if cat is None:
            raise HTTPException(status_code=404, detail="category not found")
        # Filter the global rule lists down to this category. Cheap because
        # rule tables are tiny relative to image_tags / face_cluster_assignments.
        tag_rules = [r for r in s.list_tag_category_rules() if r["category"] == name]
        cluster_rules = [r for r in s.list_cluster_category_rules() if r["category"] == name]
        return {
            "id": int(cat["id"]),
            "name": cat["name"],
            "tag_rules": tag_rules,
            "cluster_rules": cluster_rules,
        }

    @app.post("/api/categories/{name}/rules/tag")
    def api_categories_bind_tag(name: str, body: TagBindRequest) -> dict[str, Any]:
        s = _store(app)
        try:
            with s.transaction():
                s.map_tag_to_category(body.tag, name)
        except KeyError as e:
            # Unknown tag or unknown category — both are user-input errors,
            # surface as 404 so the JS can show the message inline.
            raise HTTPException(status_code=404, detail=str(e).strip("'\"")) from e
        return {"category": name, "tag": body.tag}

    @app.post("/api/categories/{name}/rules/cluster")
    def api_categories_bind_cluster(name: str, body: ClusterBindRequest) -> dict[str, Any]:
        s = _store(app)
        try:
            with s.transaction():
                s.map_face_cluster_to_category(body.cluster_id, name)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e).strip("'\"")) from e
        return {"category": name, "cluster_id": body.cluster_id}

    @app.delete("/api/categories/rules/tag/{tag_name}")
    def api_categories_unbind_tag(tag_name: str) -> dict[str, Any]:
        s = _store(app)
        with s.transaction():
            n = s.unmap_tag(tag_name)
        return {"removed": n, "tag": tag_name}

    @app.delete("/api/categories/rules/cluster/{cluster_id}")
    def api_categories_unbind_cluster(cluster_id: int) -> dict[str, Any]:
        s = _store(app)
        with s.transaction():
            n = s.unmap_face_cluster(cluster_id)
        return {"removed": n, "cluster_id": cluster_id}

    return app


# Intentionally no module-level `app` instance: importing this module must not
# open a SQLite connection or run migrations as a side effect. Use the CLI's
# `phototag serve`, which calls create_app() with the resolved settings.
