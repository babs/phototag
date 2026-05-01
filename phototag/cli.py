import json
import os
from pathlib import Path
from typing import Annotated, Any

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
def prune(
    apply: Annotated[bool, typer.Option(help="actually delete; otherwise dry-run")] = False,
    limit: Annotated[int | None, typer.Option(help="cap rows scanned")] = None,
) -> None:
    """Detect images whose file no longer exists on disk and (with --apply) drop them.

    Cluster sizes / tag counts / face assignments cascade via FK ON DELETE.
    Default is a dry-run that prints what would be removed.
    """
    settings = load_settings()
    log = get_logger("phototag.prune")
    store = Store(settings.db_path)
    counts = {"checked": 0, "missing": 0, "deleted": 0}
    missing_paths: list[str] = []
    try:
        rows = list(store.iter_images())
        if limit is not None:
            rows = rows[:limit]
        counts["checked"] = len(rows)
        for r in rows:
            if Path(r.path).exists():
                continue
            counts["missing"] += 1
            missing_paths.append(r.path)
        if apply and missing_paths:
            with store.transaction():
                for r in rows:
                    if not Path(r.path).exists():
                        store.delete_image(r.id)
                        counts["deleted"] += 1
        log.info("prune_summary", apply=apply, **counts)
        payload: dict[str, Any] = {**counts, "missing_paths": missing_paths[:50]}
        if not apply and counts["missing"]:
            payload["hint"] = f"dry-run; pass --apply to delete {counts['missing']} row(s)"
        typer.echo(json.dumps(payload, indent=2))
    finally:
        store.close()


@app.command(name="list")
def list_images(
    tag: Annotated[list[str] | None, typer.Option(help="filter by tag (repeatable, AND)")] = None,
    score_min: Annotated[float, typer.Option(help="min mean score across matched tags")] = 0.0,
    limit: Annotated[int, typer.Option(help="cap rows printed")] = 100,
    fmt: Annotated[str, typer.Option("--format", help="json | tsv")] = "json",
) -> None:
    """List images filtered by tag(s)."""
    if not tag:
        raise typer.BadParameter("--tag is required (one or more)")
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        results = store.search_images_by_tags(tag, min_score=score_min, limit=limit)
        if fmt == "tsv":
            for r in results:
                typer.echo(f"{r['id']}\t{r['score']:.3f}\t{r['path']}")
        else:
            typer.echo(json.dumps(results, indent=2, default=str))
    finally:
        store.close()


@app.command()
def stats(
    top: Annotated[int, typer.Option(help="top N tags to print")] = 50,
    kind: Annotated[str | None, typer.Option(help="filter tag kind: label | geo (default: all)")] = None,
) -> None:
    """Tag distribution + corpus counts."""
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        n_images = store.count_images()
        rows = store.list_tag_names(limit=top, kind=kind)
        n_faces = store.count_faces()
        out = {
            "images": n_images,
            "faces": n_faces,
            "top_tags": [{"name": n, "count": c} for n, c in rows],
        }
        typer.echo(json.dumps(out, indent=2))
    finally:
        store.close()


@app.command()
def export(
    fmt: Annotated[str, typer.Option("--format", help="json | csv")] = "json",
    out: Annotated[Path | None, typer.Option(help="output file (default stdout)")] = None,
    min_score: Annotated[float, typer.Option(help="drop tags below this score")] = 0.0,
) -> None:
    """Dump (image_id, path, [tags]) for the whole corpus."""
    import csv
    import io

    settings = load_settings()
    store = Store(settings.db_path)
    try:
        rows = list(store.iter_images())
        records: list[dict[str, Any]] = []
        for r in rows:
            tags = [{"name": n, "score": s} for n, s in store.list_image_tags(r.id, min_score=min_score)]
            records.append(
                {
                    "id": r.id,
                    "path": r.path,
                    "hash": r.hash,
                    "width": r.width,
                    "height": r.height,
                    "tags": tags,
                }
            )
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["id", "path", "hash", "width", "height", "tags"])
            for rec in records:
                w.writerow(
                    [
                        rec["id"],
                        rec["path"],
                        rec["hash"],
                        rec["width"],
                        rec["height"],
                        ",".join(f"{t['name']}:{t['score']:.2f}" for t in rec["tags"]),
                    ]
                )
            payload = buf.getvalue()
        else:
            payload = json.dumps(records, indent=2, default=str)
        if out is not None:
            out.write_text(payload, encoding="utf-8")
            typer.echo(f"wrote {len(records)} record(s) to {out}")
        else:
            typer.echo(payload)
    finally:
        store.close()


@app.command()
def query(
    text: Annotated[str, typer.Argument(help="free-text query")],
    limit: Annotated[int, typer.Option(help="top-K results")] = 30,
    embedder_name: Annotated[
        str | None, typer.Option("--embedder", help="embedder model_name (auto: most populated)")
    ] = None,
) -> None:
    """Semantic search by text against cached CLIP embeddings."""
    import numpy as np

    from .models.clip import ClipEmbedder

    settings = load_settings()
    store = Store(settings.db_path)
    try:
        if embedder_name is None:
            row = store.conn.execute(
                "SELECT model_name, COUNT(*) AS n FROM embeddings GROUP BY model_name ORDER BY n DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise typer.BadParameter("No embeddings in DB. Run `phototag embed` first.")
            embedder_name = row["model_name"]
        ids, mat = store.load_embeddings(embedder_name)
        if not ids:
            typer.echo("[]")
            return
        embedder = ClipEmbedder(settings.models_dir, device=settings.device)
        q = embedder.embed_texts([text])[0].astype(np.float32, copy=False)
        # CLIP image embeddings are L2-normalized; normalize the query too.
        qn = q / (np.linalg.norm(q) + 1e-12)
        mn = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12)
        scores = mn @ qn
        idx = np.argsort(-scores)[:limit]
        out = []
        for i in idx:
            iid = ids[int(i)]
            meta = store.get_image(int(iid))
            if meta is None:
                continue
            out.append({"id": iid, "path": meta["path"], "score": float(scores[int(i)])})
        typer.echo(json.dumps(out, indent=2, default=str))
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

    log = get_logger("phototag.serve")
    if host not in ("127.0.0.1", "localhost", "::1"):
        # Endpoints have no auth and serve every photo on disk by integer id;
        # binding non-loopback exposes that to the LAN. Warn loudly.
        log.warning(
            "non_loopback_bind",
            host=host,
            note="UI has no auth; do not expose to untrusted networks.",
        )

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
        scope = (
            "delete all face detections, clusters, and runs (identities + corrections kept)"
            if keep_identities
            else "delete EVERYTHING face-related: detections, clusters, runs, identities, and corrections"
        )
        typer.confirm(f"This will {scope}. Continue?", abort=True)
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        with store.transaction():
            store.purge_faces(keep_identities=keep_identities)
        typer.echo("faces purged")
    finally:
        store.close()


@faces_app.command("verify")
def faces_verify(
    min_score: Annotated[
        float, typer.Option("--min-score", help="reject faces with det_score below this")
    ] = 0.65,
    min_area: Annotated[int, typer.Option(help="reject faces whose bbox area is smaller (px²)")] = 32 * 32,
    apply: Annotated[
        bool,
        typer.Option(help="actually delete failing rows; otherwise just flag verified=0"),
    ] = False,
) -> None:
    """Heuristic verification of detected faces.

    Without --apply: marks `faces.verified` = 1 (kept) or 0 (suspect); UI shows
    suspect faces with a dashed red border so you can review.
    With --apply: deletes failing rows (cluster sizes auto-adjusted).
    """
    from .faces import verify_faces

    settings = load_settings()
    log = get_logger("phototag.faces")
    store = Store(settings.db_path)
    try:
        counts = verify_faces(store, min_det_score=min_score, min_area=min_area, apply=apply)
        log.info("faces_verify_summary", **counts)
        typer.echo(json.dumps(counts, indent=2))
    finally:
        store.close()


@faces_app.command("refine-noise")
def faces_refine_noise(
    min_size: Annotated[int, typer.Option("--min-size", help="HDBSCAN min_cluster_size")] = 3,
    min_samples: Annotated[int, typer.Option(help="HDBSCAN min_samples")] = 2,
    persist: Annotated[
        bool,
        typer.Option("--persist", help="store results as a new face_run; otherwise dry-run only"),
    ] = False,
) -> None:
    """Re-cluster orphan/noise faces and reattach identities by centroid match.

    Default is dry-run: prints the proposed clusters + how many recover their
    name via the identities table. Pass `--persist` to write a new face_run
    capturing the result. Existing named clusters are not touched.
    """
    from .faces import cluster_orphan_faces

    settings = load_settings()
    log = get_logger("phototag.faces")
    store = Store(settings.db_path)
    try:
        result = cluster_orphan_faces(
            store, min_cluster_size=min_size, min_samples=min_samples, dry_run=not persist
        )
        log.info("faces_orphan_recluster_summary", **{k: v for k, v in result.items() if k != "clusters"})
        typer.echo(json.dumps(result, indent=2, default=str))
    finally:
        store.close()


@faces_app.command("corrections")
def faces_corrections(
    action: Annotated[
        str | None,
        typer.Option(help="filter by action: named | unassigned | deleted | verified | unverified"),
    ] = None,
    face_id: Annotated[int | None, typer.Option(help="filter by face_id")] = None,
    limit: Annotated[int, typer.Option(help="cap rows printed")] = 200,
) -> None:
    """Dump the face_corrections audit log as JSON (most recent first)."""
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        rows = store.list_face_corrections(face_id=face_id, action=action)
        typer.echo(json.dumps(rows[:limit], indent=2, default=str))
    finally:
        store.close()


@faces_app.command("clear-noise-labels")
def faces_clear_noise_labels() -> None:
    """Wipe any user label sitting on a noise cluster (cluster_no=-1).

    Noise groups visually-unrelated faces; a label there mass-tags every one of
    them. Run this once to recover from the historic bug where the UI allowed
    naming noise.
    """
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        with store.transaction():
            n = store.clear_noise_cluster_labels()
        typer.echo(f"cleared label_user from {n} noise cluster row(s)")
    finally:
        store.close()


@faces_app.command("stats")
def faces_stats() -> None:
    """Quick counts: faces, clusters, runs, identities, unidentified, validated."""
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        n_faces = store.count_faces()
        n_run = store.latest_face_run() or 0
        clusters = store.list_face_clusters(n_run) if n_run else []
        n_clusters = len(clusters)
        n_named = sum(1 for c in clusters if c["label_user"])
        n_ids = len(store.list_face_identities())
        n_unidentified = store.count_unidentified_faces()
        n_user_verified = int(
            store.conn.execute("SELECT COUNT(*) AS n FROM faces WHERE user_verified=1").fetchone()["n"]
        )
        typer.echo(
            json.dumps(
                {
                    "faces": n_faces,
                    "latest_run": n_run,
                    "clusters_in_latest_run": n_clusters,
                    "named_clusters": n_named,
                    "identities": n_ids,
                    "unidentified": n_unidentified,
                    "user_verified": n_user_verified,
                },
                indent=2,
            )
        )
    finally:
        store.close()


if __name__ == "__main__":
    app()
