import hashlib
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .logging import get_logger
from .store import Store

log = get_logger(__name__)

THUMB_SIZE = 256
THUMB_PER_CLUSTER = 25


def _thumb_filename(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16] + ".jpg"


def _make_thumb(src: Path, dst: Path) -> bool:
    try:
        with Image.open(src) as img:
            # Bake EXIF Orientation into pixels — re-saved JPEG drops EXIF, so
            # without this rotation the thumb is sideways for portrait phone shots.
            rgb = ImageOps.exif_transpose(img).convert("RGB")
            rgb.thumbnail((THUMB_SIZE, THUMB_SIZE))
            rgb.save(dst, format="JPEG", quality=80)
        return True
    except Exception as e:
        log.warning("thumb_failed", src=str(src), error=str(e))
        return False


def generate_report(store: Store, *, out_dir: Path, run_id: int | None = None) -> Path:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    run_id = run_id or store.latest_cluster_run()
    if run_id is None:
        raise ValueError("No cluster run found. Run `phototag cluster` first.")

    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir = out_dir / "thumbs"
    thumbs_dir.mkdir(exist_ok=True)

    template_dir = Path(__file__).resolve().parent.parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html"]),
    )

    cluster_data: list[dict[str, Any]] = []
    total_images = 0
    for c in store.list_clusters(run_id):
        members = store.cluster_members(c["id"], limit=THUMB_PER_CLUSTER)
        thumbs: list[dict[str, str]] = []
        for _img_id, src_path, _dist in members:
            src = Path(src_path)
            if not src.exists():
                continue
            tname = _thumb_filename(src_path)
            tpath = thumbs_dir / tname
            if not tpath.exists() and not _make_thumb(src, tpath):
                continue
            thumbs.append({"thumb": f"thumbs/{tname}", "src": src.as_uri(), "name": src.name})
        cluster_data.append(
            {
                "id": c["id"],
                "cluster_no": c["cluster_no"],
                "size": c["size"],
                "label_auto": c["label_auto"] or "",
                "label_user": c["label_user"] or "",
                "thumbs": thumbs,
            }
        )
        total_images += int(c["size"])

    index_html = env.get_template("index.html.j2").render(
        run_id=run_id,
        clusters=cluster_data,
        total_images=total_images,
        n_clusters=sum(1 for c in cluster_data if int(c["cluster_no"]) != -1),
        n_noise=next((int(c["size"]) for c in cluster_data if int(c["cluster_no"]) == -1), 0),
    )
    (out_dir / "index.html").write_text(index_html, encoding="utf-8")

    cluster_tpl = env.get_template("cluster.html.j2")
    for c in cluster_data:
        (out_dir / f"cluster_{c['id']}.html").write_text(
            cluster_tpl.render(run_id=run_id, c=c), encoding="utf-8"
        )

    (out_dir / "data.json").write_text(
        json.dumps({"run_id": run_id, "clusters": cluster_data}, indent=2),
        encoding="utf-8",
    )
    log.info("report_written", index=str(out_dir / "index.html"), n_clusters=len(cluster_data))
    return out_dir / "index.html"
