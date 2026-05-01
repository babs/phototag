"""Tests for face-related Store helpers."""

from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from phototag.store import Store


def _gfc(store: Store, cluster_id: int) -> dict[str, object]:
    """Like store.get_face_cluster but asserts non-None for type checkers."""
    row = store.get_face_cluster(cluster_id)
    assert row is not None
    return row


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _add_image(store: Store, *, path: str, hash_: str = "h") -> int:
    return store.upsert_image(
        path=path,
        hash_=hash_,
        mtime=1.0,
        width=100,
        height=100,
        exif=None,
        processed_at=_now(),
    )


def _add_face(
    store: Store,
    image_id: int,
    *,
    bbox: list[int] | None = None,
    score: float = 0.9,
    embedding: np.ndarray | None = None,
) -> int:
    return store.insert_face(
        image_id=image_id,
        bbox=bbox or [10, 10, 50, 50],
        det_score=score,
        embedding=embedding if embedding is not None else np.ones(512, dtype=np.float32),
        model_name="test_model",
    )


def test_insert_and_list_face(tmp_db: Path) -> None:
    store = Store(tmp_db)
    try:
        img = _add_image(store, path="/tmp/a.jpg")
        face_id = _add_face(store, img)
        assert store.has_faces(img, "test_model")
        assert store.count_faces() == 1
        rows = store.list_faces_for_image(img)
        assert len(rows) == 1
        assert rows[0]["id"] == face_id
        assert rows[0]["bbox"] == [10, 10, 50, 50]
        assert rows[0]["verified"] is None
    finally:
        store.close()


def test_face_run_and_assignment(tmp_db: Path) -> None:
    store = Store(tmp_db)
    try:
        img = _add_image(store, path="/tmp/a.jpg")
        face_id = _add_face(store, img)
        run = store.create_face_run({"manual": True}, _now())
        cid = store.add_face_cluster(run_id=run, cluster_no=0, size=1, label_auto="x", label_user="Anne")
        store.assign_face_to_cluster(face_id, cid, distance=0.0)

        # Round-trip
        rows = store.list_faces_for_image(img)
        assert rows[0]["cluster_id"] == cid
        assert rows[0]["label_user"] == "Anne"

        people = store.list_named_people()
        assert len(people) == 1
        assert people[0]["name"] == "Anne"
        assert people[0]["count"] == 1
        assert people[0]["n_clusters"] == 1
    finally:
        store.close()


def test_search_by_persons(tmp_db: Path) -> None:
    store = Store(tmp_db)
    try:
        img1 = _add_image(store, path="/tmp/1.jpg", hash_="h1")
        img2 = _add_image(store, path="/tmp/2.jpg", hash_="h2")
        run = store.create_face_run({"manual": True}, _now())
        anne_c = store.add_face_cluster(run_id=run, cluster_no=0, size=2, label_auto=None, label_user="Anne")
        bob_c = store.add_face_cluster(run_id=run, cluster_no=1, size=1, label_auto=None, label_user="Bob")
        f1 = _add_face(store, img1)
        f2 = _add_face(store, img1, bbox=[0, 0, 60, 60])  # second face on img1
        f3 = _add_face(store, img2)
        store.assign_face_to_cluster(f1, anne_c, 0.0)
        store.assign_face_to_cluster(f2, bob_c, 0.0)
        store.assign_face_to_cluster(f3, anne_c, 0.0)

        # Anne alone → both images
        assert store.search_images_by_persons(["Anne"]) == {img1, img2}
        # Anne AND Bob → only img1
        assert store.search_images_by_persons(["Anne", "Bob"]) == {img1}
        # Empty → empty set
        assert store.search_images_by_persons([]) == set()
    finally:
        store.close()


def test_rename_clusters_by_label(tmp_db: Path) -> None:
    store = Store(tmp_db)
    try:
        run = store.create_face_run({"manual": True}, _now())
        c1 = store.add_face_cluster(run_id=run, cluster_no=0, size=1, label_auto=None, label_user="X")
        c2 = store.add_face_cluster(run_id=run, cluster_no=1, size=2, label_auto=None, label_user="X")
        # Rename both
        n = store.rename_clusters_by_label("X", "Y")
        assert n == 2
        assert _gfc(store, c1)["label_user"] == "Y"
        assert _gfc(store, c2)["label_user"] == "Y"
        # Clear
        n = store.rename_clusters_by_label("Y", None)
        assert n == 2
        assert _gfc(store, c1)["label_user"] is None
    finally:
        store.close()


def test_face_identity_upsert(tmp_db: Path) -> None:
    store = Store(tmp_db)
    try:
        v1 = np.array([1.0, 0.0], dtype=np.float32)
        store.upsert_face_identity("Anne", v1, n_samples=3)
        ids = store.list_face_identities()
        assert len(ids) == 1
        assert ids[0]["name"] == "Anne"
        assert ids[0]["n_samples"] == 3
        np.testing.assert_array_equal(ids[0]["centroid"], v1)
        # Update
        v2 = np.array([0.0, 1.0], dtype=np.float32)
        store.upsert_face_identity("Anne", v2, n_samples=5)
        ids = store.list_face_identities()
        assert ids[0]["n_samples"] == 5
        np.testing.assert_array_equal(ids[0]["centroid"], v2)
        store.delete_face_identity("Anne")
        assert store.list_face_identities() == []
    finally:
        store.close()


def test_delete_face_decrements_cluster_size(tmp_db: Path) -> None:
    store = Store(tmp_db)
    try:
        img = _add_image(store, path="/tmp/a.jpg")
        run = store.create_face_run({}, _now())
        cid = store.add_face_cluster(run_id=run, cluster_no=0, size=2, label_auto=None)
        f1 = _add_face(store, img)
        f2 = _add_face(store, img, bbox=[20, 20, 40, 40])
        store.assign_face_to_cluster(f1, cid, 0.0)
        store.assign_face_to_cluster(f2, cid, 0.0)
        # Pre: cluster size = 2
        assert _gfc(store, cid)["size"] == 2
        store.delete_face(f1)
        assert store.count_faces() == 1
        # Cluster size decremented
        assert _gfc(store, cid)["size"] == 1
    finally:
        store.close()


def test_cluster_centroid(tmp_db: Path) -> None:
    store = Store(tmp_db)
    try:
        img = _add_image(store, path="/tmp/a.jpg")
        run = store.create_face_run({}, _now())
        cid = store.add_face_cluster(run_id=run, cluster_no=0, size=2, label_auto=None)
        v1 = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        v2 = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        f1 = store.insert_face(image_id=img, bbox=[0, 0, 10, 10], det_score=0.9, embedding=v1, model_name="m")
        f2 = store.insert_face(image_id=img, bbox=[0, 0, 10, 10], det_score=0.9, embedding=v2, model_name="m")
        store.assign_face_to_cluster(f1, cid, 0.0)
        store.assign_face_to_cluster(f2, cid, 0.0)
        c = store.cluster_centroid(cid)
        assert c is not None
        np.testing.assert_allclose(c, [0.5, 0.5, 0.0])
        # Empty cluster → None
        empty_cid = store.add_face_cluster(run_id=run, cluster_no=1, size=0, label_auto=None)
        assert store.cluster_centroid(empty_cid) is None
    finally:
        store.close()


def test_unassign_face_from_run(tmp_db: Path) -> None:
    store = Store(tmp_db)
    try:
        img = _add_image(store, path="/tmp/a.jpg")
        run = store.create_face_run({}, _now())
        cid = store.add_face_cluster(run_id=run, cluster_no=0, size=1, label_auto=None)
        f1 = _add_face(store, img)
        store.assign_face_to_cluster(f1, cid, 0.0)
        n = store.unassign_face_from_run(f1, run)
        assert n == 1
        rows = store.list_faces_for_image(img)
        assert rows[0]["cluster_id"] is None
        # Cluster size decremented
        assert _gfc(store, cid)["size"] == 0
    finally:
        store.close()


def test_attach_face_to_best_identity(tmp_db: Path) -> None:
    """High-confidence match attaches AND auto-validates; marginal match
    attaches without validating; no match returns None."""
    from phototag.faces import attach_face_to_best_identity

    store = Store(tmp_db)
    try:
        anne = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        store.upsert_face_identity("Anne", anne, n_samples=5)
        img = _add_image(store, path="/tmp/a.jpg")

        # 1) Strong match (sim ≈ 0.998) → attached + auto-validated.
        emb = np.array([0.95, 0.05, 0.0], dtype=np.float32)
        fid = store.insert_face(
            image_id=img, bbox=[0, 0, 10, 10], det_score=0.9, embedding=emb, model_name="m"
        )
        hit = attach_face_to_best_identity(store, fid, emb, image_id=img)
        assert hit is not None and hit["name"] == "Anne"
        assert hit["user_verified"] is True
        rows = store.list_faces_for_image(img)
        assert rows[0]["label_user"] == "Anne"
        assert rows[0]["user_verified"] == 1
        # Audit row carries image_id (no post-hoc UPDATE needed).
        audit = [a for a in store.list_face_corrections(face_id=fid) if a["action"] == "named"]
        assert audit and audit[0]["image_id"] == img

        # 2) Marginal match (sim = 0.6, between 0.5 and 0.7) → attached, not validated.
        marginal = np.array([0.6, 0.8, 0.0], dtype=np.float32)
        # Cosine to anne ([1,0,0]) = 0.6 / sqrt(1) / sqrt(0.36+0.64) = 0.6
        fid2 = store.insert_face(
            image_id=img, bbox=[40, 40, 10, 10], det_score=0.9, embedding=marginal, model_name="m"
        )
        hit2 = attach_face_to_best_identity(
            store, fid2, marginal, image_id=img, threshold=0.5, auto_verify_threshold=0.7
        )
        assert hit2 is not None and hit2["name"] == "Anne"
        assert hit2["user_verified"] is False

        # 3) No identity in range → None, stays orphan.
        far = np.array([0.0, 0.0, 1.0], dtype=np.float32)
        fid3 = store.insert_face(
            image_id=img, bbox=[20, 20, 10, 10], det_score=0.9, embedding=far, model_name="m"
        )
        assert attach_face_to_best_identity(store, fid3, far, image_id=img, threshold=0.5) is None

        # 4) dim-mismatched identity is skipped silently (defensive).
        store.upsert_face_identity("WrongDim", np.array([1.0, 0.0], dtype=np.float32), n_samples=1)
        fid4 = store.insert_face(
            image_id=img,
            bbox=[60, 60, 10, 10],
            det_score=0.9,
            embedding=np.array([0.99, 0.01, 0.01], dtype=np.float32),
            model_name="m",
        )
        # Should still resolve to Anne (3-d), ignoring WrongDim (2-d).
        hit4 = attach_face_to_best_identity(
            store, fid4, np.array([0.99, 0.01, 0.01], dtype=np.float32), image_id=img
        )
        assert hit4 is not None and hit4["name"] == "Anne"

        # 5) Empty identities table → None.
        store.delete_face_identity("Anne")
        store.delete_face_identity("WrongDim")
        fid5 = store.insert_face(
            image_id=img,
            bbox=[80, 80, 10, 10],
            det_score=0.9,
            embedding=np.array([1.0, 0.0, 0.0], dtype=np.float32),
            model_name="m",
        )
        assert attach_face_to_best_identity(store, fid5, np.array([1.0, 0.0, 0.0], dtype=np.float32)) is None
    finally:
        store.close()


def test_auto_attach_orphans_dry_run(tmp_db: Path) -> None:
    """Dry-run reports per-identity histogram + auto-validate counts; no DB writes."""
    from phototag.faces import auto_attach_orphans

    store = Store(tmp_db)
    try:
        # Two identities at orthogonal centroids.
        store.upsert_face_identity("Anne", np.array([1.0, 0.0, 0.0], dtype=np.float32), n_samples=5)
        store.upsert_face_identity("Bob", np.array([0.0, 1.0, 0.0], dtype=np.float32), n_samples=5)
        img = _add_image(store, path="/tmp/a.jpg")
        # 3 strong-Anne, 2 strong-Bob, 1 nowhere.
        for _ in range(3):
            store.insert_face(
                image_id=img,
                bbox=[0, 0, 10, 10],
                det_score=0.9,
                embedding=np.array([0.99, 0.01, 0.0], dtype=np.float32),
                model_name="insightface_buffalo_l_v1",
            )
        for _ in range(2):
            store.insert_face(
                image_id=img,
                bbox=[20, 20, 10, 10],
                det_score=0.9,
                embedding=np.array([0.01, 0.99, 0.0], dtype=np.float32),
                model_name="insightface_buffalo_l_v1",
            )
        store.insert_face(
            image_id=img,
            bbox=[40, 40, 10, 10],
            det_score=0.9,
            embedding=np.array([0.0, 0.0, 1.0], dtype=np.float32),
            model_name="insightface_buffalo_l_v1",
        )

        result = auto_attach_orphans(store, dry_run=True)
        assert result["dry_run"] is True
        assert result["n_orphan"] == 6
        assert result["matched"] == 5
        assert result["below_threshold"] == 1
        assert "Anne" in result["by_identity"]
        assert "Bob" in result["by_identity"]
        assert result["by_identity"]["Anne"]["n"] == 3
        assert result["by_identity"]["Bob"]["n"] == 2
        # All matched faces are at sim ≈ 1.0 → all auto-validated.
        assert result["auto_validated"] == 5
        # Confirm dry-run wrote nothing: still no cluster assignments.
        assert store.conn.execute("SELECT COUNT(*) AS n FROM face_cluster_assignments").fetchone()["n"] == 0
    finally:
        store.close()


def test_attach_helper_detaches_from_noise(tmp_db: Path) -> None:
    """A face attached via the helper must lose its noise-cluster membership
    so the noise size and the unidentified workspace stay accurate."""
    from phototag.faces import attach_face_to_best_identity

    store = Store(tmp_db)
    try:
        store.upsert_face_identity("Anne", np.array([1.0, 0.0, 0.0], dtype=np.float32), n_samples=5)
        img = _add_image(store, path="/tmp/a.jpg")
        # Insert a face + assign to a noise cluster.
        run = store.create_face_run({}, _now())
        noise_cid = store.add_face_cluster(
            run_id=run, cluster_no=-1, size=1, label_auto="noise", label_user=None
        )
        emb = np.array([0.95, 0.05, 0.0], dtype=np.float32)
        fid = store.insert_face(
            image_id=img, bbox=[0, 0, 10, 10], det_score=0.9, embedding=emb, model_name="m"
        )
        store.assign_face_to_cluster(fid, noise_cid, distance=0.0)
        assert _gfc(store, noise_cid)["size"] == 1

        hit = attach_face_to_best_identity(store, fid, emb, image_id=img)
        assert hit is not None and hit["name"] == "Anne"

        # Noise membership gone; noise size decremented.
        cur = store.conn.execute(
            "SELECT cluster_id FROM face_cluster_assignments WHERE face_id=?", (fid,)
        ).fetchall()
        cluster_ids = {int(r["cluster_id"]) for r in cur}
        assert noise_cid not in cluster_ids
        assert _gfc(store, noise_cid)["size"] == 0
    finally:
        store.close()


def test_attach_helper_refuses_ambiguous_top2(tmp_db: Path) -> None:
    """Two near-equal centroids → bail rather than guess."""
    from phototag.faces import attach_face_to_best_identity

    store = Store(tmp_db)
    try:
        # Two identities along orthogonal axes; query equidistant from both.
        store.upsert_face_identity("Anne", np.array([1.0, 0.0], dtype=np.float32), n_samples=5)
        store.upsert_face_identity("Bea", np.array([0.0, 1.0], dtype=np.float32), n_samples=5)
        img = _add_image(store, path="/tmp/x.jpg")
        # Equidistant: cos(sim, Anne) ≈ cos(sim, Bea) ≈ 0.707
        emb = np.array([0.707, 0.707], dtype=np.float32)
        fid = store.insert_face(
            image_id=img, bbox=[0, 0, 10, 10], det_score=0.9, embedding=emb, model_name="m"
        )
        # Default min_margin=0.05 — top-1 vs top-2 are within ~0 → bail.
        assert attach_face_to_best_identity(store, fid, emb, image_id=img) is None

        # Lowering margin to 0 forces an attach; sanity-check the bail path
        # was not a false negative.
        hit = attach_face_to_best_identity(store, fid, emb, image_id=img, min_margin=0.0)
        assert hit is not None
    finally:
        store.close()


def test_auto_attach_orphans_image_id_in_audit(tmp_db: Path) -> None:
    """Bulk path must populate face_corrections.image_id, not leave NULL."""
    from phototag.faces import auto_attach_orphans

    store = Store(tmp_db)
    try:
        store.upsert_face_identity("Anne", np.array([1.0, 0.0, 0.0], dtype=np.float32), n_samples=5)
        img = _add_image(store, path="/tmp/a.jpg")
        fid = store.insert_face(
            image_id=img,
            bbox=[0, 0, 10, 10],
            det_score=0.9,
            embedding=np.array([0.99, 0.01, 0.0], dtype=np.float32),
            model_name="insightface_buffalo_l_v1",
        )
        result = auto_attach_orphans(store, dry_run=False)
        assert result["matched"] == 1
        rows = store.list_face_corrections(face_id=fid, action="named")
        assert rows and rows[0]["image_id"] == img
    finally:
        store.close()


def test_auto_attach_orphans_persist(tmp_db: Path) -> None:
    from phototag.faces import auto_attach_orphans

    store = Store(tmp_db)
    try:
        store.upsert_face_identity("Anne", np.array([1.0, 0.0, 0.0], dtype=np.float32), n_samples=5)
        img = _add_image(store, path="/tmp/a.jpg")
        fid = store.insert_face(
            image_id=img,
            bbox=[0, 0, 10, 10],
            det_score=0.9,
            embedding=np.array([0.99, 0.01, 0.0], dtype=np.float32),
            model_name="insightface_buffalo_l_v1",
        )
        result = auto_attach_orphans(store, dry_run=False)
        assert result["matched"] == 1
        rows = store.list_faces_for_image(img)
        assert rows[0]["label_user"] == "Anne"
        assert rows[0]["user_verified"] == 1
        # attach_sim is ≈ 0.9999
        assert rows[0]["attach_sim"] is not None
        assert rows[0]["attach_sim"] > 0.99
        # face_id was the orphan; persist hits attach_face_to_best_identity for each.
        assert fid == rows[0]["id"]
    finally:
        store.close()


def test_cannot_link_identities_for_face_helper(tmp_db: Path) -> None:
    """`unassigned` corrections referencing a labelled cluster contribute the
    cluster's label_user to the per-face cannot-link set; NULL labels and
    other actions are ignored."""
    store = Store(tmp_db)
    try:
        img = _add_image(store, path="/tmp/a.jpg")
        run = store.create_face_run({}, _now())
        anne_c = store.add_face_cluster(run_id=run, cluster_no=0, size=1, label_auto=None, label_user="Anne")
        unlabeled_c = store.add_face_cluster(
            run_id=run, cluster_no=1, size=1, label_auto=None, label_user=None
        )
        bea_c = store.add_face_cluster(run_id=run, cluster_no=2, size=1, label_auto=None, label_user="Bea")
        fid = _add_face(store, img)
        # Rejected Anne (labelled) → joins cannot-link.
        store.log_face_correction(face_id=fid, image_id=img, action="unassigned", cluster_id=anne_c)
        # Rejected an unlabeled cluster → ignored.
        store.log_face_correction(face_id=fid, image_id=img, action="unassigned", cluster_id=unlabeled_c)
        # `named` Bea is not an unassign → ignored even though Bea has a label.
        store.log_face_correction(face_id=fid, image_id=img, action="named", cluster_id=bea_c, name="Bea")
        assert store.cannot_link_identities_for_face(fid) == {"Anne"}
        # Bulk variant: face with no rows is absent from the dict.
        bulk = store.cannot_link_identities_for_faces([fid, fid + 9999])
        assert bulk == {fid: {"Anne"}}
    finally:
        store.close()


def test_attach_skips_cannot_link_identity(tmp_db: Path) -> None:
    """Per-face attach: an `unassigned` correction against Anne (rejected)
    forces the helper to pick the next-best identity (Bea) even though
    cosine ranks Anne higher."""
    from phototag.faces import attach_face_to_best_identity

    store = Store(tmp_db)
    try:
        # Two strong identities. The face embedding favors Anne.
        store.upsert_face_identity("Anne", np.array([1.0, 0.0, 0.0], dtype=np.float32), n_samples=5)
        store.upsert_face_identity("Bea", np.array([0.0, 1.0, 0.0], dtype=np.float32), n_samples=5)
        img = _add_image(store, path="/tmp/a.jpg")
        # Embedding strongly Anne (cos≈0.9), weakly Bea (cos≈0.4).
        emb = np.array([0.9, 0.4, 0.0], dtype=np.float32)
        fid = store.insert_face(
            image_id=img, bbox=[0, 0, 10, 10], det_score=0.9, embedding=emb, model_name="m"
        )
        # Sanity: without the cannot-link, Anne wins.
        baseline = attach_face_to_best_identity(store, fid, emb, image_id=img, min_margin=0.0)
        assert baseline is not None and baseline["name"] == "Anne"

        # Reset state for the cannot-link case: clear the manual cluster
        # membership the previous attach created and seed the rejection.
        store.unassign_face_globally(fid)
        run = store.create_face_run({}, _now())
        anne_old = store.add_face_cluster(
            run_id=run, cluster_no=0, size=1, label_auto=None, label_user="Anne"
        )
        store.log_face_correction(face_id=fid, image_id=img, action="unassigned", cluster_id=anne_old)

        # Anne is now in the cannot-link set → helper falls through to Bea.
        # min_margin=0.0 so Bea (cos≈0.4 vs nothing else) clears the bar.
        hit = attach_face_to_best_identity(store, fid, emb, image_id=img, threshold=0.3, min_margin=0.0)
        assert hit is not None
        assert hit["name"] == "Bea"
    finally:
        store.close()


def test_attach_returns_none_when_only_identity_is_blocked(tmp_db: Path) -> None:
    """When the only candidate identity is in the cannot-link set, the
    helper returns None instead of attaching."""
    from phototag.faces import attach_face_to_best_identity

    store = Store(tmp_db)
    try:
        store.upsert_face_identity("Anne", np.array([1.0, 0.0, 0.0], dtype=np.float32), n_samples=5)
        img = _add_image(store, path="/tmp/a.jpg")
        emb = np.array([0.99, 0.01, 0.0], dtype=np.float32)
        fid = store.insert_face(
            image_id=img, bbox=[0, 0, 10, 10], det_score=0.9, embedding=emb, model_name="m"
        )
        run = store.create_face_run({}, _now())
        anne_old = store.add_face_cluster(
            run_id=run, cluster_no=0, size=1, label_auto=None, label_user="Anne"
        )
        store.log_face_correction(face_id=fid, image_id=img, action="unassigned", cluster_id=anne_old)
        assert attach_face_to_best_identity(store, fid, emb, image_id=img) is None
    finally:
        store.close()


def test_auto_attach_orphans_honours_cannot_link(tmp_db: Path) -> None:
    """Bulk auto-attach: a per-face cannot-link blocks Anne; the orphan
    falls through to Bea (or stays orphan if no fallback). The result dict
    surfaces `cannot_link_skipped`."""
    from phototag.faces import auto_attach_orphans

    store = Store(tmp_db)
    try:
        store.upsert_face_identity("Anne", np.array([1.0, 0.0, 0.0], dtype=np.float32), n_samples=5)
        store.upsert_face_identity("Bea", np.array([0.0, 1.0, 0.0], dtype=np.float32), n_samples=5)
        img = _add_image(store, path="/tmp/a.jpg")
        # Two orphans. Both favor Anne but the second one falls back to Bea
        # comfortably; one third strongly-Anne face is left untouched as a
        # control to verify only the rejected face is steered away.
        e_blocked = np.array([0.9, 0.4, 0.0], dtype=np.float32)
        e_control = np.array([0.99, 0.01, 0.0], dtype=np.float32)
        fid_blocked = store.insert_face(
            image_id=img,
            bbox=[0, 0, 10, 10],
            det_score=0.9,
            embedding=e_blocked,
            model_name="insightface_buffalo_l_v1",
        )
        fid_control = store.insert_face(
            image_id=img,
            bbox=[20, 20, 10, 10],
            det_score=0.9,
            embedding=e_control,
            model_name="insightface_buffalo_l_v1",
        )
        # Reject Anne for the blocked face.
        run = store.create_face_run({}, _now())
        anne_old = store.add_face_cluster(
            run_id=run, cluster_no=0, size=1, label_auto=None, label_user="Anne"
        )
        store.log_face_correction(face_id=fid_blocked, image_id=img, action="unassigned", cluster_id=anne_old)

        result = auto_attach_orphans(store, dry_run=False, threshold=0.3, min_margin=0.0)
        assert result["cannot_link_skipped"] >= 1
        # Both faces matched: control → Anne, blocked → Bea.
        rows = {r["id"]: r for r in store.list_faces_for_image(img)}
        assert rows[fid_control]["label_user"] == "Anne"
        assert rows[fid_blocked]["label_user"] == "Bea"
    finally:
        store.close()


def test_purge_faces(tmp_db: Path) -> None:
    store = Store(tmp_db)
    try:
        img = _add_image(store, path="/tmp/a.jpg")
        _add_face(store, img)
        store.upsert_face_identity("Anne", np.ones(2, dtype=np.float32), 1)
        store.purge_faces(keep_identities=True)
        assert store.count_faces() == 0
        assert len(store.list_face_identities()) == 1
        store.purge_faces()
        assert len(store.list_face_identities()) == 0
    finally:
        store.close()
