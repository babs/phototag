import json
import os
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .config import ClusterConfig
from .logging import get_logger, setup_logging
from .settings import load as load_settings
from .store import Store

app = typer.Typer(
    name="phototag",
    help="Local photo tagging and clustering.",
    no_args_is_help=True,
)


def _bootstrap() -> None:
    settings = load_settings()
    setup_logging(log_level=settings.log_level, json_logs=settings.json_logs)
    log = get_logger("phototag")
    log.info(
        "startup",
        version=os.getenv("VERSION", __version__),
        commit_hash=os.getenv("COMMIT_HASH", "00000000-dirty"),
        build_timestamp=os.getenv("BUILD_TIMESTAMP", "1970-01-01T00:00:00+00:00"),
        project_url=os.getenv("PROJECT_URL", "unknown"),
    )


@app.callback()
def _main() -> None:
    _bootstrap()


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


@app.command()
def scan(
    path: Annotated[Path, typer.Argument(help="Folder to scan recursively")],
    threshold: Annotated[float, typer.Option(help="Min RAM++ score to keep")] = 0.68,
    batch_size: Annotated[int, typer.Option(help="Inference batch size")] = 16,
    force: Annotated[bool, typer.Option(help="Rehash + re-tag everything")] = False,
    force_tag: Annotated[bool, typer.Option(help="Re-tag without rehashing")] = False,
) -> None:
    """Scan a folder, hash, and tag images with RAM++."""
    from .models.ram import RamTagger
    from .pipeline import scan_and_tag

    settings = load_settings()
    log = get_logger("phototag.scan")
    store = Store(settings.db_path)
    try:
        tagger = RamTagger(settings.models_dir, threshold=threshold, device=settings.device)
        counts = scan_and_tag(path, store, tagger, batch_size=batch_size, force=force, force_tag=force_tag)
        log.info("scan_done", **counts)
    finally:
        store.close()


@app.command()
def embed(
    path: Annotated[
        Path | None,
        typer.Argument(help="Optional folder to scan first; otherwise embeds existing rows"),
    ] = None,
    batch_size: Annotated[int, typer.Option(help="Embedding batch size")] = 32,
    force: Annotated[bool, typer.Option(help="Recompute all embeddings")] = False,
) -> None:
    """Compute CLIP embeddings for all images in the DB."""
    from .models.clip import ClipEmbedder
    from .pipeline import embed_all

    if path is not None:
        typer.echo(f"Note: pass {path} to `scan` first; embed only operates on DB rows.")

    settings = load_settings()
    log = get_logger("phototag.embed")
    store = Store(settings.db_path)
    try:
        embedder = ClipEmbedder(settings.models_dir, device=settings.device)
        counts = embed_all(store, embedder, batch_size=batch_size, force=force)
        log.info("embed_done", **counts)
    finally:
        store.close()


@app.command()
def cluster(
    min_cluster_size: Annotated[int, typer.Option("--min-size", help="HDBSCAN min cluster size")] = 20,
    min_samples: Annotated[int, typer.Option(help="HDBSCAN min samples")] = 5,
    embedder_name: Annotated[
        str | None, typer.Option("--embedder", help="Embedder name (auto: most populated)")
    ] = None,
) -> None:
    """Run UMAP+HDBSCAN over stored CLIP embeddings."""
    from .clustering import cluster as run_cluster

    settings = load_settings()
    log = get_logger("phototag.cluster")
    store = Store(settings.db_path)
    try:
        if embedder_name is None:
            row = store.conn.execute(
                "SELECT model_name, COUNT(*) AS n FROM embeddings GROUP BY model_name ORDER BY n DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise typer.BadParameter("No embeddings in DB. Run `phototag embed` first.")
            embedder_name = row["model_name"]
        cfg = ClusterConfig(
            hdbscan_min_cluster_size=min_cluster_size,
            hdbscan_min_samples=min_samples,
        )
        run_id = run_cluster(store, embedder_name=embedder_name, config=cfg)
        log.info("cluster_done", run_id=run_id)
        typer.echo(f"cluster run {run_id} created")
    finally:
        store.close()


@app.command()
def report(
    out: Annotated[Path, typer.Option(help="Output directory")] = Path("report"),
    run_id: Annotated[int | None, typer.Option(help="Cluster run id (default: latest)")] = None,
) -> None:
    """Generate an HTML report for a cluster run."""
    from .reporting import generate_report

    settings = load_settings()
    store = Store(settings.db_path)
    try:
        index = generate_report(store, out_dir=out, run_id=run_id)
        typer.echo(str(index))
    finally:
        store.close()


@app.command()
def info(
    image_path: Annotated[Path, typer.Argument(help="Image file path")],
) -> None:
    """Inspect tags + metadata for a single image."""
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        row = store.get_image_by_path(str(image_path.resolve()))
        if row is None:
            typer.echo(f"not in DB: {image_path}", err=True)
            raise typer.Exit(1)
        tags = store.list_tags_for_image(row.id)
        out = {
            "id": row.id,
            "path": row.path,
            "hash": row.hash,
            "size": [row.width, row.height],
            "tags": [{"name": n, "score": s, "model": m} for n, s, m in tags],
        }
        typer.echo(json.dumps(out, indent=2))
    finally:
        store.close()


@app.command()
def rename(
    cluster_id: Annotated[int, typer.Argument(help="cluster.id (not cluster_no)")],
    label: Annotated[
        str | None,
        typer.Argument(help="new user label; omit or pass '' to clear"),
    ] = None,
) -> None:
    """Set or clear `label_user` on a cluster row."""
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        if store.get_cluster(cluster_id) is None:
            typer.echo(f"cluster {cluster_id} not found", err=True)
            raise typer.Exit(1)
        with store.transaction():
            store.set_cluster_label_user(cluster_id, label or None)
        typer.echo(f"cluster {cluster_id} label_user={label!r}")
    finally:
        store.close()


@app.command(name="rename-bulk")
def rename_bulk(
    json_path: Annotated[Path, typer.Argument(help="JSON map {cluster_id: label_user}")],
) -> None:
    """Bulk-rename clusters from a JSON file."""
    import json as _json

    mapping = _json.loads(json_path.read_text())
    settings = load_settings()
    store = Store(settings.db_path)
    n = 0
    try:
        with store.transaction():
            for k, v in mapping.items():
                cid = int(k)
                if store.get_cluster(cid) is None:
                    typer.echo(f"  skip: cluster {cid} not found", err=True)
                    continue
                store.set_cluster_label_user(cid, v if v else None)
                n += 1
    finally:
        store.close()
    typer.echo(f"renamed {n}")


@app.command(name="exif-backfill")
def exif_backfill(
    limit: Annotated[int | None, typer.Option(help="optional row cap")] = None,
    force: Annotated[bool, typer.Option(help="re-extract even if exif_json already populated")] = False,
) -> None:
    """Walk DB rows, extract EXIF from disk, persist into images.exif_json."""
    from .exif import extract_exif

    settings = load_settings()
    log = get_logger("phototag.exif")
    store = Store(settings.db_path)
    counts = {"total": 0, "updated": 0, "no_exif": 0, "failed": 0, "skipped": 0}
    try:
        rows = list(store.iter_images())
        counts["total"] = len(rows)
        if limit is not None:
            rows = rows[:limit]
        with store.transaction():
            for r in rows:
                if not force and store.get_image_exif(r.id):
                    counts["skipped"] += 1
                    continue
                try:
                    exif = extract_exif(Path(r.path))
                except Exception as e:
                    log.warning("exif_extract_failed", path=r.path, error=str(e))
                    counts["failed"] += 1
                    continue
                if exif:
                    store.update_image_exif(r.id, exif)
                    counts["updated"] += 1
                else:
                    counts["no_exif"] += 1
        log.info("exif_backfill_done", **counts)
        typer.echo(json.dumps(counts, indent=2))
    finally:
        store.close()


@app.command(name="geo-tag")
def geo_tag(
    limit: Annotated[int | None, typer.Option(help="optional row cap")] = None,
    force: Annotated[bool, typer.Option(help="re-tag even if geo tags already present")] = False,
) -> None:
    """Reverse-geocode EXIF GPS and add city/country tags (model_name=geo_v1)."""
    from .geo import reverse_lookup, to_tags

    settings = load_settings()
    log = get_logger("phototag.geo")
    store = Store(settings.db_path)
    counts = {
        "considered": 0,
        "tagged": 0,
        "no_gps": 0,
        "failed": 0,
        "skipped": 0,
    }
    try:
        rows = list(store.iter_images())
        if limit is not None:
            rows = rows[:limit]
        with store.transaction():
            for r in rows:
                exif = store.get_image_exif(r.id) or {}
                gps = exif.get("gps")
                if not gps:
                    counts["no_gps"] += 1
                    continue
                counts["considered"] += 1
                if not force:
                    has = store.conn.execute(
                        "SELECT 1 FROM image_tags WHERE image_id=? AND model_name='geo_v1' LIMIT 1",
                        (r.id,),
                    ).fetchone()
                    if has:
                        counts["skipped"] += 1
                        continue
                geo = reverse_lookup(float(gps["lat"]), float(gps["lon"]))
                if not geo:
                    counts["failed"] += 1
                    continue
                tags = to_tags(geo)
                if not tags:
                    counts["failed"] += 1
                    continue
                store.replace_image_tags(r.id, "geo_v1", tags)
                counts["tagged"] += 1
        log.info("geo_tag_done", **counts)
        typer.echo(json.dumps(counts, indent=2))
    finally:
        store.close()


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="bind address")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="port")] = 8000,
) -> None:
    """Start the FastAPI UI to browse clusters and rename them."""
    import uvicorn

    from .ui import create_app

    settings = load_settings()
    fapp = create_app(db_path=settings.db_path)
    uvicorn.run(fapp, host=host, port=port, log_config=None, access_log=False)


# ---- faces (opt-in; see specs/15-faces.md) ----

faces_app = typer.Typer(help="Face detection / recognition (opt-in, biometric).")
app.add_typer(faces_app, name="faces")


_FACES_GATE_KEY = "faces_consent"


def _faces_gate(store: Store, i_understand: bool) -> None:
    row = store.conn.execute("SELECT value FROM meta WHERE key=?", (_FACES_GATE_KEY,)).fetchone()
    if row:
        return
    if not i_understand:
        typer.echo(
            "Face features process biometric data. Embeddings are stored locally "
            "and never leave this machine. Re-run with --i-understand to enable.",
            err=True,
        )
        raise typer.Exit(2)
    store.conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_FACES_GATE_KEY, "yes"),
    )


@faces_app.command("detect")
def faces_detect(
    limit: Annotated[int | None, typer.Option(help="optional row cap")] = None,
    force: Annotated[bool, typer.Option(help="re-detect even if faces present")] = False,
    i_understand: Annotated[
        bool,
        typer.Option(
            "--i-understand",
            help="acknowledge biometric data on first run (one-time gate)",
        ),
    ] = False,
) -> None:
    """Detect faces in every image and persist embeddings."""
    from .faces import FaceDetector, detect_faces_all

    settings = load_settings()
    log = get_logger("phototag.faces")
    store = Store(settings.db_path)
    try:
        _faces_gate(store, i_understand)
        det = FaceDetector(settings.models_dir, device=settings.device)
        counts = detect_faces_all(store, det, force=force, limit=limit)
        log.info("faces_detect_summary", **counts)
        typer.echo(json.dumps(counts, indent=2))
    finally:
        store.close()


@faces_app.command("cluster")
def faces_cluster(
    min_size: Annotated[int, typer.Option("--min-size", help="HDBSCAN min_cluster_size")] = 3,
    min_samples: Annotated[int, typer.Option(help="HDBSCAN min_samples")] = 2,
) -> None:
    """Cluster face embeddings into people; carry forward existing names."""
    from .faces import cluster_faces

    settings = load_settings()
    log = get_logger("phototag.faces")
    store = Store(settings.db_path)
    try:
        run_id = cluster_faces(store, min_cluster_size=min_size, min_samples=min_samples)
        log.info("faces_cluster_run", run_id=run_id)
        typer.echo(f"face cluster run {run_id} created")
    finally:
        store.close()


@faces_app.command("name")
def faces_name(
    cluster_id: Annotated[int, typer.Argument(help="face_clusters.id")],
    label: Annotated[str, typer.Argument(help="person name")],
) -> None:
    """Name a face cluster; the name persists across re-clustering runs."""
    from .faces import name_cluster

    settings = load_settings()
    store = Store(settings.db_path)
    try:
        name_cluster(store, cluster_id, label)
        typer.echo(f"face cluster {cluster_id} → {label!r}")
    finally:
        store.close()


@faces_app.command("unname")
def faces_unname(
    cluster_id: Annotated[int, typer.Argument(help="face_clusters.id")],
) -> None:
    """Clear the user-assigned name on a face cluster (does not touch identities)."""
    from .faces import name_cluster

    settings = load_settings()
    store = Store(settings.db_path)
    try:
        name_cluster(store, cluster_id, None)
        typer.echo(f"face cluster {cluster_id} unnamed")
    finally:
        store.close()


@faces_app.command("purge")
def faces_purge(
    keep_identities: Annotated[
        bool, typer.Option(help="keep face_identities table; drop everything else")
    ] = False,
    yes: Annotated[bool, typer.Option(help="skip confirmation")] = False,
) -> None:
    """Drop all face data."""
    if not yes:
        typer.confirm(
            "This will delete all face detections, clusters, and identities. Continue?",
            abort=True,
        )
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        with store.transaction():
            store.purge_faces(keep_identities=keep_identities)
        typer.echo("faces purged")
    finally:
        store.close()


@faces_app.command("stats")
def faces_stats() -> None:
    """Quick counts: faces, clusters, runs, identities."""
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        n_faces = store.count_faces()
        n_run = store.latest_face_run() or 0
        n_clusters = len(store.list_face_clusters(n_run)) if n_run else 0
        n_named = sum(1 for c in store.list_face_clusters(n_run) if c["label_user"]) if n_run else 0
        n_ids = len(store.list_face_identities())
        typer.echo(
            json.dumps(
                {
                    "faces": n_faces,
                    "latest_run": n_run,
                    "clusters_in_latest_run": n_clusters,
                    "named_clusters": n_named,
                    "identities": n_ids,
                },
                indent=2,
            )
        )
    finally:
        store.close()


if __name__ == "__main__":
    app()
