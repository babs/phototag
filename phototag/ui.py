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

    if api_token:
        # Lightweight shared-secret guard. Disabled by default (the local-
        # loopback case). When set, every request except the index page,
        # /healthz, /static/* and CORS preflights (OPTIONS) must carry the
        # token via either:
        #   - X-API-Token header (preferred for fetch/XHR)
        #   - ?token=<value> query string (so <img> loads work in the SPA)
        # The token is also injected into the index template so the JS can
        # read it and decorate fetch/asset URLs automatically.
        import secrets as _secrets

        from starlette.middleware.base import BaseHTTPMiddleware

        _PUBLIC = ("/", "/healthz", "/favicon.ico")
        _expected_token: str = api_token  # narrow for mypy under closure

        class _AuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next: Any) -> Any:
                p = request.url.path
                # Let CORS preflight reach CORSMiddleware (which is registered
                # as inner middleware here) without needing a token. Native
                # asset loads (img/css) and the SPA shell are also public.
                if request.method == "OPTIONS" or p in _PUBLIC or p.startswith("/static/"):
                    return await call_next(request)
                got = request.headers.get("X-API-Token") or request.query_params.get("token", "") or ""
                # constant-time comparison so a timing oracle can't probe the
                # token byte-by-byte.
                if not _secrets.compare_digest(got, _expected_token):
                    return Response(status_code=401, content="missing or wrong API token")
                return await call_next(request)

        app.add_middleware(_AuthMiddleware)
        log.info("api_token_enabled")

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
            {"image_id": iid, "path": p, "distance": d}
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
        src = Path(meta["path"])
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
            # detected "Alex" face gets the name without waiting for a
            # full `phototag faces cluster` run. High-confidence matches
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
        `keep_face_id` (the verified one). Use after the user marks the real
        detection of "Alex" verified to clean up the false-positive triple."""
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
            src = Path(meta["path"])
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
        src = Path(meta["path"])
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
            if crow:
                cid = int(crow["id"])
                s.assign_face_to_cluster(face_id, cid, distance=0.0)
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
                s.assign_face_to_cluster(face_id, cid, distance=0.0)

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

                with Image.open(meta["path"]) as src_img:
                    cropped = crop_face(src_img, face["bbox"])
                    cropped.thumbnail((FACE_THUMB_SIZE, FACE_THUMB_SIZE))
                    cropped.save(dst, format="JPEG", quality=85)
            except Exception as e:
                log.warning("face_thumb_failed", face_id=face_id, error=str(e))
                raise HTTPException(status_code=500, detail="thumb failed") from e
        return FileResponse(dst, media_type="image/jpeg", headers=_CACHE_HEADERS)

    return app


# Intentionally no module-level `app` instance: importing this module must not
# open a SQLite connection or run migrations as a side effect. Use the CLI's
# `phototag serve`, which calls create_app() with the resolved settings.
