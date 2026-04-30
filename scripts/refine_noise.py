#!/usr/bin/env python3
"""Re-cluster the noise points from an existing cluster run.

Pulls every image whose latest cluster_no == -1, runs UMAP+HDBSCAN with
permissive parameters on just that subset, and prints (cluster_no, size,
top-tags) for the new groupings. Read-only against the DB unless --persist
is given, in which case a new cluster_run is recorded.
"""

import argparse
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phototag.store import Store  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="data/full.db")
    p.add_argument("--run-id", type=int, default=None, help="source run; default: latest")
    p.add_argument("--min-size", type=int, default=5)
    p.add_argument("--min-samples", type=int, default=2)
    p.add_argument("--n-neighbors", type=int, default=15)
    p.add_argument("--n-components", type=int, default=30)
    p.add_argument("--top-n", type=int, default=10, help="rows to print per cluster")
    p.add_argument("--persist", action="store_true", help="store as a new cluster_run")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    store = Store(args.db)
    try:
        run_id = args.run_id or store.latest_cluster_run()
        if run_id is None:
            print("no cluster runs found", file=sys.stderr)
            sys.exit(2)

        # Pull image ids of noise points in the source run.
        noise_rows = store.conn.execute(
            """
            SELECT ic.image_id
            FROM image_clusters ic
            JOIN clusters c ON c.id = ic.cluster_id
            WHERE c.run_id=? AND c.cluster_no=-1
            """,
            (run_id,),
        ).fetchall()
        noise_ids = [int(r["image_id"]) for r in noise_rows]
        if not noise_ids:
            print(f"run {run_id} has no noise; nothing to refine")
            return
        print(f"source run={run_id}, noise count={len(noise_ids)}")

        # Pull their embeddings (any model — pick the most populated).
        model_row = store.conn.execute(
            "SELECT model_name FROM embeddings GROUP BY model_name ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        if model_row is None:
            print("no embeddings in DB", file=sys.stderr)
            sys.exit(2)
        model_name = model_row["model_name"]
        all_ids, all_vecs = store.load_embeddings(model_name)
        idx_by_id = {iid: i for i, iid in enumerate(all_ids)}
        rows = [idx_by_id[i] for i in noise_ids if i in idx_by_id]
        if not rows:
            print("no embeddings for noise points", file=sys.stderr)
            sys.exit(2)
        sub_ids = [all_ids[i] for i in rows]
        sub_vecs = all_vecs[rows]
        print(f"embedding model={model_name}, vectors={sub_vecs.shape}")

        import hdbscan
        import umap

        n_components = min(args.n_components, max(2, sub_vecs.shape[0] - 2))
        n_neighbors = min(args.n_neighbors, max(2, sub_vecs.shape[0] - 1))
        reducer = umap.UMAP(
            n_components=n_components,
            n_neighbors=n_neighbors,
            min_dist=0.0,
            metric="cosine",
            random_state=42,
        )
        reduced = reducer.fit_transform(sub_vecs)
        labels = hdbscan.HDBSCAN(
            min_cluster_size=args.min_size,
            min_samples=args.min_samples,
            metric="euclidean",
            cluster_selection_method="eom",
        ).fit_predict(reduced)

        # Group + size + top tags via TF-IDF-ish (just frequency here, simple).
        members: dict[int, list[int]] = {}
        for iid, lbl in zip(sub_ids, labels, strict=True):
            members.setdefault(int(lbl), []).append(iid)

        n_clusters = sum(1 for k in members if k != -1)
        n_noise = len(members.get(-1, []))
        print(f"refined: {n_clusters} clusters, {n_noise} still-noise")
        print()

        per_image_tags = store.tags_per_image(sub_ids)
        ordered = sorted((k for k in members if k != -1), key=lambda k: -len(members[k]))
        print(f"{'#':>4} {'size':>5}  top tags")
        print(f"{'-' * 4:>4} {'-' * 5:>5}  {'-' * 60}")
        for cno in ordered[: args.top_n]:
            ids = members[cno]
            tag_counts: Counter[str] = Counter()
            for iid in ids:
                for t in per_image_tags.get(iid, []):
                    tag_counts[t] += 1
            top_tags = [t for t, _ in tag_counts.most_common(5)]
            print(f"{cno:>4} {len(ids):>5}  {', '.join(top_tags)}")

        if args.persist:
            params = {
                "type": "noise_refinement",
                "source_run_id": run_id,
                "embedder": model_name,
                "n_vectors": int(sub_vecs.shape[0]),
                "umap": {
                    "n_components": n_components,
                    "n_neighbors": n_neighbors,
                    "min_dist": 0.0,
                    "metric": "cosine",
                    "random_state": 42,
                },
                "hdbscan": {
                    "min_cluster_size": args.min_size,
                    "min_samples": args.min_samples,
                    "metric": "euclidean",
                    "selection": "eom",
                },
            }
            with store.transaction():
                new_run = store.create_cluster_run(params, datetime.now(UTC).isoformat(timespec="seconds"))
                centroids = {
                    lv: reduced[[i for i, lab in enumerate(labels) if lab == lv]].mean(axis=0)
                    for lv in members
                    if lv != -1
                }
                for lv in sorted(members):
                    ids = members[lv]
                    label_auto = (
                        ", ".join(
                            t
                            for t, _ in Counter(
                                t for iid in ids for t in per_image_tags.get(iid, [])
                            ).most_common(5)
                        )
                        if lv != -1
                        else "noise"
                    )
                    cid = store.add_cluster(
                        run_id=new_run,
                        cluster_no=lv,
                        size=len(ids),
                        label_auto=label_auto or None,
                    )
                    if lv == -1:
                        for iid in ids:
                            store.assign_image_to_cluster(iid, cid, distance=0.0)
                    else:
                        c = centroids[lv]
                        for iid in ids:
                            i = sub_ids.index(iid)
                            d = float(np.linalg.norm(reduced[i] - c))
                            store.assign_image_to_cluster(iid, cid, distance=d)
            print(f"\npersisted as cluster_run id={new_run}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
