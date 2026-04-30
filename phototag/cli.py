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


if __name__ == "__main__":
    app()
