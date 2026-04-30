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

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
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
THUMB_CACHE = Path("data/thumbs-cache")
PREVIEW_CACHE = Path("data/previews-cache")


class RenameRequest(BaseModel):
    label_user: str | None


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

    app = FastAPI(title="phototag UI")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

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
        return templates.TemplateResponse(request, "ui.html", {"title": "phototag"})

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
        min_score: Annotated[float, Query(ge=0.0, le=1.0)] = 0.0,
        limit: Annotated[int, Query(ge=1, le=500)] = 120,
        run_id: int | None = None,
    ) -> list[dict[str, Any]]:
        if not tag:
            return []
        s = _store(app)
        # Default to the most recent run so we don't return one row per image per run.
        rid = run_id if run_id is not None else s.latest_cluster_run()
        return s.search_images_by_tags(tag, min_score=min_score, limit=limit, run_id=rid)

    @app.get("/api/images/{image_id}")
    def api_image(image_id: int) -> dict[str, Any]:
        s = _store(app)
        meta = s.get_image(image_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="image not found")
        tags = [{"name": n, "score": sc} for n, sc in s.list_image_tags(image_id)]
        exif = s.get_image_exif(image_id)
        return {**meta, "tags": tags, "exif": exif}

    # Long-lived cache: image content is keyed by image_id which is stable;
    # if the file changes the row gets replaced (and id reused only on rescan).
    _CACHE_HEADERS = {"Cache-Control": "public, max-age=86400, immutable"}

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

    return app


# Module-level app instance for `uvicorn phototag.ui:app` invocation.
app = create_app()
