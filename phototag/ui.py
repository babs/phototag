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


def _resized(src: Path, dst: Path, max_side: int, *, quality: int = 82) -> bool:
    try:
        with Image.open(src) as img:
            # exif_transpose applies the EXIF Orientation tag (rotate/flip) so the
            # bytes match the visual orientation. Strip-on-save means we must bake
            # the rotation into the pixels here, not rely on EXIF later.
            rgb = ImageOps.exif_transpose(img).convert("RGB")
            rgb.thumbnail((max_side, max_side))
            rgb.save(dst, format="JPEG", quality=quality)
        return True
    except Exception as e:
        log.warning("resize_failed", src=str(src), max_side=max_side, error=str(e))
        return False


def create_app(db_path: Path | None = None) -> FastAPI:
    settings = load_settings()
    setup_logging(log_level=settings.log_level, json_logs=settings.json_logs)
    db = db_path or settings.db_path
    THUMB_CACHE.mkdir(parents=True, exist_ok=True)
    PREVIEW_CACHE.mkdir(parents=True, exist_ok=True)
    FACE_THUMB_CACHE.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="phototag UI")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    project_root = Path(__file__).resolve().parent.parent
    templates = Jinja2Templates(directory=str(project_root / "templates"))
    app.mount("/static", StaticFiles(directory=str(project_root / "static")), name="static")

    @app.on_event("startup")
    def _open_store() -> None:
        app.state.store = Store(db)
        log.info("ui_started", db=str(db))

    @app.on_event("shutdown")
    def _close_store() -> None:
        s = getattr(app.state, "store", None)
        if s is not None:
            s.close()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        # `version` busts /static cache on every restart so a regenerated
        # ui.css/ui.js takes effect on the next reload.
        from time import time as _time

        return templates.TemplateResponse(
            request,
            "ui.html",
            {"title": "phototag", "version": int(_time())},
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

    @app.post("/api/images/{image_id}/redetect-faces")
    def api_redetect_faces(image_id: int) -> dict[str, Any]:
        """Re-run face detection on a single image. Replaces any existing faces."""
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
        det = getattr(app.state, "face_detector", None)
        if det is None:
            settings = load_settings()
            det = FaceDetector(settings.models_dir, device=settings.device)
            app.state.face_detector = det
        try:
            with _Image.open(src) as img:
                img.load()
                faces = det.detect(img)
        except Exception as e:
            log.error("face_redetect_failed", path=str(src), error=str(e))
            raise HTTPException(status_code=500, detail=f"detect failed: {e}") from e
        with s.transaction():
            s.delete_all_faces_for_image(image_id)
            for f in faces:
                s.insert_face(
                    image_id=image_id,
                    bbox=f.bbox,
                    det_score=f.det_score,
                    embedding=f.embedding,
                    model_name=MODEL_NAME,
                    landmarks=f.landmarks,
                )
        log.info("face_redetected", image_id=image_id, faces=len(faces))
        return {"image_id": image_id, "faces": len(faces)}

    @app.delete("/api/faces/{face_id}")
    def api_delete_face(face_id: int) -> dict[str, Any]:
        """Drop a false-positive face row."""
        s = _store(app)
        if s.get_face(face_id) is None:
            raise HTTPException(status_code=404, detail="face not found")
        with s.transaction():
            s.delete_face(face_id)
        log.info("face_deleted", face_id=face_id)
        return {"deleted": face_id}

    @app.post("/api/faces/{face_id}/unassign")
    def api_unassign_face(face_id: int) -> dict[str, Any]:
        """Remove this face's cluster assignment in the latest run.

        Use case: face was clustered to the wrong person. The face row stays
        so the next `phototag faces cluster` can re-place it.
        """
        s = _store(app)
        if s.get_face(face_id) is None:
            raise HTTPException(status_code=404, detail="face not found")
        latest = s.latest_face_run()
        with s.transaction():
            if latest is None:
                # No runs scoped — drop every assignment for this face.
                rows = s.conn.execute(
                    "SELECT cluster_id FROM face_cluster_assignments WHERE face_id=?",
                    (face_id,),
                ).fetchall()
                cids = [int(r["cluster_id"]) for r in rows]
                s.conn.execute("DELETE FROM face_cluster_assignments WHERE face_id=?", (face_id,))
                for cid in cids:
                    s.conn.execute(
                        "UPDATE face_clusters SET size = MAX(0, size - 1) WHERE id=?",
                        (cid,),
                    )
                removed = len(cids)
            else:
                removed = s.unassign_face_from_run(face_id, latest)
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

    def _face_color(cluster_id: int | None) -> str:
        if cluster_id is None:
            return "hsl(0, 0%, 70%)"
        return f"hsl({(cluster_id * 137) % 360}, 70%, 55%)"

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
        # An image can have rows from multiple cluster runs; latest run wins.
        latest = s.latest_face_run()
        for f in s.list_faces_for_image(image_id):
            if f["id"] in seen:
                continue
            # Prefer the row that matches the latest run (if any).
            if latest is not None and f["cluster_id"] is not None:
                cluster = s.get_face_cluster(int(f["cluster_id"]))
                if cluster and cluster["run_id"] != latest:
                    continue
            seen.add(f["id"])
            out.append(
                {
                    "id": f["id"],
                    "bbox": f["bbox"],
                    "det_score": f["det_score"],
                    "verified": f["verified"],
                    "cluster_id": f["cluster_id"],
                    "cluster_no": f["cluster_no"],
                    "label": f["label_user"] or f["label_auto"],
                    "named": f["label_user"] is not None,
                    "color": _face_color(f["cluster_id"]),
                }
            )
        return out

    @app.get("/api/people")
    def api_people(
        run_id: int | None = None,
        only_named: bool = False,
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
            members = s.face_cluster_members(c["id"], limit=3)
            out.append(
                {
                    "cluster_id": c["id"],
                    "cluster_no": c["cluster_no"],
                    "name": c["label_user"],
                    "auto": c["label_auto"],
                    "size": c["size"],
                    "color": _face_color(c["id"]),
                    "samples": [{"face_id": m["face_id"], "image_id": m["image_id"]} for m in members],
                }
            )
        return out

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
            "color": _face_color(cluster_id),
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

        from datetime import UTC
        from datetime import datetime as _dt

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

        log.info("face_named_manual", face_id=face_id, cluster_id=cid, name=name)
        return {"ok": True, "face_id": face_id, "cluster_id": cid, "name": name}

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


# Module-level app instance for `uvicorn phototag.ui:app` invocation.
app = create_app()
