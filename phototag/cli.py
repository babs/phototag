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
        # Existence check is the long pole on a network-mounted corpus
        # (each stat() is a round-trip). Run in a thread pool so 16 stats
        # overlap their latency. Local filesystems also win marginally.
        from concurrent.futures import ThreadPoolExecutor

        missing_ids: list[int] = []
        with ThreadPoolExecutor(max_workers=16) as ex:
            results = list(ex.map(lambda r: (r.id, r.path, Path(r.path).exists()), rows))
        for image_id, path, present in results:
            if not present:
                missing_ids.append(image_id)
                missing_paths.append(path)
        counts["missing"] = len(missing_ids)
        if apply and missing_ids:
            with store.transaction():
                for image_id in missing_ids:
                    store.delete_image(image_id)
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
    if fmt not in ("json", "tsv"):
        raise typer.BadParameter("--format must be 'json' or 'tsv'")
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
    if kind is not None and kind not in ("label", "geo"):
        raise typer.BadParameter("--kind must be 'label' or 'geo'")
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

    if fmt not in ("json", "csv"):
        raise typer.BadParameter("--format must be 'json' or 'csv'")
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        # Single bulk SELECT instead of N+1 list_image_tags() calls so a 10k-
        # row export takes one round-trip instead of 10k.
        rows = list(store.iter_images())
        cur = store.conn.execute(
            """
            SELECT it.image_id, t.name, it.score
            FROM image_tags it JOIN tags t ON t.id = it.tag_id
            WHERE it.score >= ?
            ORDER BY it.image_id, it.score DESC
            """,
            (float(min_score),),
        )
        tags_by_image: dict[int, list[dict[str, Any]]] = {}
        for row in cur:
            tags_by_image.setdefault(int(row["image_id"]), []).append(
                {"name": row["name"], "score": float(row["score"])}
            )
        records: list[dict[str, Any]] = [
            {
                "id": r.id,
                "path": r.path,
                "hash": r.hash,
                "width": r.width,
                "height": r.height,
                "tags": tags_by_image.get(r.id, []),
            }
            for r in rows
        ]
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
    """Semantic search by text against cached CLIP embeddings.

    Note: the first call boots the CLIP text encoder (~2-3 s on CPU). Long
    queries are truncated by the CLIP tokenizer at 77 tokens; a one-line
    warning is printed when the input exceeds ~200 chars.
    """
    import numpy as np

    from .models.clip import ClipEmbedder

    if len(text) > 200:
        typer.echo(
            f"warn: query is {len(text)} chars; CLIP truncates at 77 tokens (~250 chars). "
            "consider shortening for tighter relevance.",
            err=True,
        )

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
def doctor(
    fix: Annotated[bool, typer.Option(help="apply auto-fixable issues; otherwise dry-run")] = False,
) -> None:
    """Walk the DB and report inconsistencies. JSON out, fixable items marked.

    Checks:
      - cluster size mismatch (face_clusters.size vs COUNT(assignments))
      - clusters.size mismatch (image clusters)
      - faces with no embedding (NULL or 0-dim BLOB)
      - identities with no live cluster mention (label_user not present
        in any face_clusters)
      - schema_version vs MIGRATIONS length
    `--fix` only fixes the safe cases (cluster.size recompute).
    """
    from . import store as _store_mod

    settings = load_settings()
    log = get_logger("phototag.doctor")
    store = Store(settings.db_path)
    issues: dict[str, Any] = {}
    try:
        # The five diagnostic SELECTs are independent and read-only — fan
        # them out in a thread pool so a 100k-face DB doesn't serialize the
        # full-table scans (each thread gets its own SQLite connection via
        # Store's thread-local pool, and WAL allows concurrent readers).
        from concurrent.futures import ThreadPoolExecutor

        def _q_schema() -> tuple[str, Any]:
            row = store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            cur_ver = int(row["value"]) if row else 0
            expected = len(_store_mod.MIGRATIONS)
            return (
                ("schema_version", {"current": cur_ver, "expected": expected})
                if cur_ver != expected
                else ("", None)
            )

        def _q_face_cluster_size() -> tuple[str, Any]:
            rows = store.conn.execute(
                """
                SELECT fc.id AS cluster_id, fc.size AS recorded,
                       IFNULL((SELECT COUNT(*) FROM face_cluster_assignments fca
                               WHERE fca.cluster_id = fc.id), 0) AS actual
                FROM face_clusters fc
                WHERE fc.size != IFNULL((SELECT COUNT(*) FROM face_cluster_assignments fca
                                         WHERE fca.cluster_id = fc.id), 0)
                """
            ).fetchall()
            return ("face_cluster_size_mismatch", [dict(r) for r in rows]) if rows else ("", None)

        def _q_img_cluster_size() -> tuple[str, Any]:
            rows = store.conn.execute(
                """
                SELECT c.id AS cluster_id, c.size AS recorded,
                       IFNULL((SELECT COUNT(*) FROM image_clusters ic
                               WHERE ic.cluster_id = c.id), 0) AS actual
                FROM clusters c
                WHERE c.size != IFNULL((SELECT COUNT(*) FROM image_clusters ic
                                        WHERE ic.cluster_id = c.id), 0)
                """
            ).fetchall()
            return ("image_cluster_size_mismatch", [dict(r) for r in rows]) if rows else ("", None)

        def _q_no_emb() -> tuple[str, Any]:
            rows = store.conn.execute(
                "SELECT id FROM faces WHERE embedding IS NULL OR LENGTH(embedding) = 0"
            ).fetchall()
            return ("faces_no_embedding", [int(r["id"]) for r in rows][:50]) if rows else ("", None)

        def _q_orphan_idents() -> tuple[str, Any]:
            rows = store.conn.execute(
                """
                SELECT fi.name FROM face_identities fi
                WHERE NOT EXISTS (
                    SELECT 1 FROM face_clusters fc WHERE fc.label_user = fi.name
                )
                """
            ).fetchall()
            return ("orphan_identities", [r["name"] for r in rows]) if rows else ("", None)

        with ThreadPoolExecutor(max_workers=5) as ex:
            for key, value in ex.map(
                lambda f: f(),
                [_q_schema, _q_face_cluster_size, _q_img_cluster_size, _q_no_emb, _q_orphan_idents],
            ):
                if key:
                    issues[key] = value

        bad_face_clusters = issues.get("face_cluster_size_mismatch") or []
        bad_img_clusters = issues.get("image_cluster_size_mismatch") or []

        if fix and bad_face_clusters:
            with store.transaction():
                for r in bad_face_clusters:
                    store.conn.execute(
                        "UPDATE face_clusters SET size = ? WHERE id = ?",
                        (int(r["actual"]), int(r["cluster_id"])),
                    )
            issues["face_cluster_size_fixed"] = len(bad_face_clusters)
        if fix and bad_img_clusters:
            with store.transaction():
                for r in bad_img_clusters:
                    store.conn.execute(
                        "UPDATE clusters SET size = ? WHERE id = ?",
                        (int(r["actual"]), int(r["cluster_id"])),
                    )
            issues["image_cluster_size_fixed"] = len(bad_img_clusters)

        ok = (
            not any(k for k in issues if not k.endswith("_fixed") and k != "schema_version")
            and "schema_version" not in issues
        )
        out = {"ok": ok, "issues": issues, "fix_applied": bool(fix)}
        log.info("doctor_summary", **{"ok": ok, "n_issue_kinds": len(issues)})
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


@faces_app.command("auto-attach")
def faces_auto_attach(
    threshold: Annotated[float, typer.Option(help="min cosine sim to attach to a known identity")] = 0.5,
    auto_verify_threshold: Annotated[
        float,
        typer.Option(
            "--auto-verify-threshold",
            help="sim ≥ this also flips user_verified=1 (default 0.7)",
        ),
    ] = 0.7,
    limit: Annotated[int | None, typer.Option(help="cap orphan rows scanned")] = None,
    persist: Annotated[bool, typer.Option("--persist", help="actually attach; default is dry-run")] = False,
) -> None:
    """Bulk-attach orphan faces to known identities by centroid match.

    Default is a dry-run that prints projected per-identity counts +
    similarity ranges. `--persist` writes assignments + audit rows + flips
    user_verified for high-confidence matches. Heavy lifting is one
    vectorized cosine matmul (`(N_orphans, D) @ (D, N_idents)`); on a
    1500-orphan / 50-identity DB this is well under a second.
    """
    from .faces import auto_attach_orphans

    settings = load_settings()
    log = get_logger("phototag.faces")
    store = Store(settings.db_path)
    try:
        result = auto_attach_orphans(
            store,
            threshold=threshold,
            auto_verify_threshold=auto_verify_threshold,
            limit=limit,
            dry_run=not persist,
        )
        log.info("faces_auto_attach_summary", **{k: v for k, v in result.items() if k != "by_identity"})
        typer.echo(json.dumps(result, indent=2, default=str))
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
