"""Smoke tests for the FastAPI UI endpoints.

Uses a temp DB; no model loads.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from phototag.store import Store
from phototag.ui import create_app


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@pytest.fixture
def seeded_db(tmp_db: Path) -> Path:
    """A DB pre-populated with images, tags, faces, and a face cluster."""
    s = Store(tmp_db)
    try:
        img1 = s.upsert_image(
            path="/tmp/a.jpg",
            hash_="h1",
            mtime=1.0,
            width=10,
            height=10,
            exif={"make": "samsung"},
            processed_at=_now(),
        )
        img2 = s.upsert_image(
            path="/tmp/b.jpg",
            hash_="h2",
            mtime=2.0,
            width=10,
            height=10,
            exif=None,
            processed_at=_now(),
        )
        s.replace_image_tags(img1, "ram_v1", [("cat", 0.9), ("smile", 0.8)])
        s.replace_image_tags(img2, "ram_v1", [("dog", 0.95)])
        run = s.create_face_run({"manual": True}, _now())
        cid = s.add_face_cluster(run_id=run, cluster_no=0, size=1, label_auto="x", label_user="Anne")
        face = s.insert_face(
            image_id=img1,
            bbox=[0, 0, 50, 50],
            det_score=0.9,
            embedding=np.ones(512, dtype=np.float32),
            model_name="m",
        )
        s.assign_face_to_cluster(face, cid, 0.0)
    finally:
        s.close()
    return tmp_db


@pytest.fixture
def client(seeded_db: Path) -> Iterator[TestClient]:
    app = create_app(db_path=seeded_db)
    with TestClient(app) as c:
        yield c


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_runs_empty_when_only_face_run_exists(client: TestClient) -> None:
    """`/api/runs` lists *image* cluster runs only; the seeded face_run must
    not show up here. Guards against regressions that would join the wrong
    table and surface face runs in the cluster picker."""
    r = client.get("/api/runs")
    assert r.status_code == 200
    assert r.json() == []


def test_tags_endpoint(client: TestClient) -> None:
    r = client.get("/api/tags")
    assert r.status_code == 200
    names = {x["name"] for x in r.json()}
    assert {"cat", "smile", "dog"} <= names


def test_search_by_tag(client: TestClient) -> None:
    r = client.get("/api/search?tag=cat")
    assert r.status_code == 200
    paths = {x["path"] for x in r.json()}
    assert "/tmp/a.jpg" in paths
    assert "/tmp/b.jpg" not in paths


def test_search_empty_returns_empty(client: TestClient) -> None:
    r = client.get("/api/search")
    assert r.status_code == 200
    assert r.json() == []


def test_people_names(client: TestClient) -> None:
    r = client.get("/api/people/names")
    assert r.status_code == 200
    body = r.json()
    assert any(p["name"] == "Anne" and p["count"] == 1 for p in body)


def test_search_by_person(client: TestClient) -> None:
    r = client.get("/api/search?person=Anne")
    assert r.status_code == 200
    paths = {x["path"] for x in r.json()}
    assert paths == {"/tmp/a.jpg"}


def _image_id(s: Store, path: str) -> int:
    row = s.get_image_by_path(path)
    assert row is not None
    return row.id


def _add_face(s: Store, image_id: int, *, bbox: list[int] | None = None) -> int:
    return s.insert_face(
        image_id=image_id,
        bbox=bbox or [10, 10, 30, 30],
        det_score=0.9,
        embedding=np.ones(512, dtype=np.float32),
        model_name="m",
    )


def test_image_faces(client: TestClient, seeded_db: Path) -> None:
    s = Store(seeded_db)
    img_id = _image_id(s, "/tmp/a.jpg")
    s.close()
    r = client.get(f"/api/images/{img_id}/faces")
    assert r.status_code == 200
    faces = r.json()
    assert len(faces) == 1
    assert faces[0]["named"] is True
    assert faces[0]["label"] == "Anne"


def test_face_name_creates_manual_cluster(client: TestClient, seeded_db: Path) -> None:
    s = Store(seeded_db)
    img_id = _image_id(s, "/tmp/b.jpg")
    # Insert a fresh unclustered face on img2.
    new_face = s.insert_face(
        image_id=img_id,
        bbox=[1, 1, 30, 30],
        det_score=0.9,
        embedding=np.array([0.5] * 512, dtype=np.float32),
        model_name="m",
    )
    s.close()
    r = client.post(f"/api/faces/{new_face}/name", json={"name": "Bob"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Bob"
    # Now /api/people/names should include Bob.
    r2 = client.get("/api/people/names")
    names = {p["name"] for p in r2.json()}
    assert "Bob" in names


def test_rename_all_then_split(client: TestClient, seeded_db: Path) -> None:
    s = Store(seeded_db)
    # Add a second cluster also named Anne so we have 2.
    run = s.latest_face_run()
    assert run is not None
    img_id = _image_id(s, "/tmp/b.jpg")
    cid2 = s.add_face_cluster(run_id=run, cluster_no=99, size=1, label_auto=None, label_user="Anne")
    f2 = s.insert_face(
        image_id=img_id,
        bbox=[1, 1, 20, 20],
        det_score=0.9,
        embedding=np.array([0.1] * 512, dtype=np.float32),
        model_name="m",
    )
    s.assign_face_to_cluster(f2, cid2, 0.0)
    s.close()

    # Rename all Anne → Lee (2 clusters)
    r = client.post("/api/people/by-name/Anne/rename", json={"name": "Lee"})
    assert r.status_code == 200
    assert r.json()["renamed"] == 2

    # Split Lee → Lee 1 / Lee 2
    r = client.post("/api/people/by-name/Lee/split", json={})
    assert r.status_code == 200
    assert r.json()["split"] == 2

    # Now /api/people/names should have Lee 1, Lee 2 (not Lee).
    r2 = client.get("/api/people/names").json()
    names = {p["name"] for p in r2}
    assert "Lee 1" in names
    assert "Lee 2" in names
    assert "Lee" not in names


def test_delete_face(client: TestClient, seeded_db: Path) -> None:
    s = Store(seeded_db)
    n_before = s.count_faces()
    img_id = _image_id(s, "/tmp/a.jpg")
    face = s.list_faces_for_image(img_id)[0]["id"]
    s.close()

    r = client.delete(f"/api/faces/{face}")
    assert r.status_code == 200
    s = Store(seeded_db)
    assert s.count_faces() == n_before - 1
    s.close()


def test_drop_all_image_faces(client: TestClient, seeded_db: Path) -> None:
    s = Store(seeded_db)
    img_id = _image_id(s, "/tmp/a.jpg")
    s.close()
    r = client.delete(f"/api/images/{img_id}/faces")
    assert r.status_code == 200
    assert r.json()["deleted"] >= 1


def test_by_name_merged_view(client: TestClient, seeded_db: Path) -> None:
    """`/api/people/by-name/{name}` walks every cluster sharing the label."""
    s = Store(seeded_db)
    # Add a second Anne cluster on img2 so the merged view aggregates two.
    run = s.latest_face_run()
    assert run is not None
    img_id = _image_id(s, "/tmp/b.jpg")
    cid2 = s.add_face_cluster(run_id=run, cluster_no=42, size=1, label_auto=None, label_user="Anne")
    f2 = s.insert_face(
        image_id=img_id,
        bbox=[0, 0, 30, 30],
        det_score=0.9,
        embedding=np.array([0.7] * 512, dtype=np.float32),
        model_name="m",
    )
    s.assign_face_to_cluster(f2, cid2, 0.0)
    s.close()
    r = client.get("/api/people/by-name/Anne")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Anne"
    assert body["n_clusters"] == 2
    assert body["n_photos"] == 2
    assert len(body["groups"]) == 2


def test_by_name_edge_view(client: TestClient, seeded_db: Path) -> None:
    """`/api/people/by-name/{name}/edge` returns the N farthest faces of the
    person, sorted DESC by distance, across every cluster sharing the label.
    """
    s = Store(seeded_db)
    run = s.latest_face_run()
    assert run is not None
    img2_id = _image_id(s, "/tmp/b.jpg")
    img1_id = _image_id(s, "/tmp/a.jpg")
    cid2 = s.add_face_cluster(run_id=run, cluster_no=7, size=1, label_auto=None, label_user="Anne")
    f_far = _add_face(s, img2_id, bbox=[1, 1, 10, 10])
    f_mid = _add_face(s, img1_id, bbox=[2, 2, 10, 10])
    s.assign_face_to_cluster(f_far, cid2, 0.95)
    s.assign_face_to_cluster(f_mid, cid2, 0.40)
    s.close()
    r = client.get("/api/people/by-name/Anne/edge?limit=2")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    # Sorted DESC by distance: 0.95 first, then 0.40.
    assert rows[0]["face_id"] == f_far
    assert rows[0]["distance"] >= rows[1]["distance"]
    assert rows[1]["face_id"] == f_mid
    # Each row carries the per-face triage payload.
    for row in rows:
        for k in (
            "face_id",
            "image_id",
            "path",
            "bbox",
            "distance",
            "distance_kind",
            "cluster_id",
            "cluster_no",
        ):
            assert k in row


def test_by_name_edge_unknown_name_404(client: TestClient) -> None:
    r = client.get("/api/people/by-name/Nobody/edge")
    assert r.status_code == 404


def test_only_unnamed_filter(client: TestClient, seeded_db: Path) -> None:
    """`/api/people?only_unnamed=true` returns only clusters lacking label_user."""
    s = Store(seeded_db)
    run = s.latest_face_run()
    assert run is not None
    s.add_face_cluster(run_id=run, cluster_no=99, size=0, label_auto="person 99", label_user=None)
    s.close()
    r = client.get("/api/people?only_unnamed=true")
    assert r.status_code == 200
    rows = r.json()
    assert all(p["name"] is None for p in rows)


def test_corrections_logged_on_unassign(client: TestClient, seeded_db: Path) -> None:
    """Wrong-cluster action records a face_corrections row."""
    s = Store(seeded_db)
    img_id = _image_id(s, "/tmp/a.jpg")
    face = s.list_faces_for_image(img_id)[0]["id"]
    old_cluster = s.list_faces_for_image(img_id)[0]["cluster_id"]
    s.close()
    r = client.post(f"/api/faces/{face}/unassign", json={})
    assert r.status_code == 200
    audit = client.get("/api/faces/corrections?action=unassigned").json()
    assert any(c["face_id"] == face and c["cluster_id"] == old_cluster for c in audit)


def test_corrections_logged_on_delete(client: TestClient, seeded_db: Path) -> None:
    s = Store(seeded_db)
    img_id = _image_id(s, "/tmp/a.jpg")
    face = s.list_faces_for_image(img_id)[0]["id"]
    s.close()
    r = client.delete(f"/api/faces/{face}")
    assert r.status_code == 200
    audit = client.get("/api/faces/corrections?action=deleted").json()
    assert any(c["face_id"] == face for c in audit)


def test_naming_noise_cluster_refused(client: TestClient, seeded_db: Path) -> None:
    """Naming a noise cluster (cluster_no=-1) must fail — it would mass-tag
    every unrelated face in the noise bag. Clearing is still allowed."""
    s = Store(seeded_db)
    run = s.latest_face_run()
    assert run is not None
    noise = s.add_face_cluster(run_id=run, cluster_no=-1, size=10, label_auto="noise", label_user=None)
    s.close()
    r = client.post(f"/api/people/{noise}/name", json={"name": "Trap"})
    assert r.status_code == 400
    # Clearing (None) must still succeed.
    r = client.post(f"/api/people/{noise}/name", json={"name": None})
    assert r.status_code == 200


def test_clear_noise_labels(client: TestClient, seeded_db: Path) -> None:
    s = Store(seeded_db)
    run = s.latest_face_run()
    assert run is not None
    # Force a noise cluster to carry a (historically-buggy) label.
    noise = s.add_face_cluster(run_id=run, cluster_no=-1, size=5, label_auto="noise", label_user=None)
    s.conn.execute("UPDATE face_clusters SET label_user='Carol' WHERE id=?", (noise,))
    s.close()
    r = client.post("/api/faces/clear-noise-labels", json={})
    assert r.status_code == 200
    assert r.json()["cleared"] == 1
    s = Store(seeded_db)
    row = s.conn.execute("SELECT label_user FROM face_clusters WHERE id=?", (noise,)).fetchone()
    assert row["label_user"] is None
    s.close()


def test_face_verify_and_drop_dups(client: TestClient, seeded_db: Path) -> None:
    """User can verify one face on a photo and drop the other same-name dups."""
    s = Store(seeded_db)
    run = s.latest_face_run()
    assert run is not None
    img_id = _image_id(s, "/tmp/a.jpg")
    # Add two more faces on the same image, both labelled Anne (the seeded one
    # is also labelled Anne via the seed fixture).
    cid = s.add_face_cluster(run_id=run, cluster_no=2, size=2, label_auto=None, label_user="Anne")
    f2 = _add_face(s, img_id, bbox=[60, 60, 30, 30])
    f3 = _add_face(s, img_id, bbox=[100, 100, 30, 30])
    s.assign_face_to_cluster(f2, cid, 0.0)
    s.assign_face_to_cluster(f3, cid, 0.0)
    s.close()
    # Verify f2 (the "good" one).
    r = client.post(f"/api/faces/{f2}/verify", json={})
    assert r.status_code == 200
    # Drop the other two Annes on this image, keep f2.
    r = client.delete(f"/api/images/{img_id}/faces/dups-of/Anne?keep_face_id={f2}")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] >= 1
    s = Store(seeded_db)
    remaining = [f for f in s.list_faces_for_image(img_id) if f.get("label_user") == "Anne"]
    assert any(f["id"] == f2 for f in remaining)
    s.close()


def test_manual_name_detaches_from_noise(client: TestClient, seeded_db: Path) -> None:
    """Naming a noise-cluster face must drop its noise assignment so the noise
    cluster size is correct and the face leaves the unidentified pool."""
    s = Store(seeded_db)
    run = s.latest_face_run()
    assert run is not None
    img_id = _image_id(s, "/tmp/b.jpg")
    noise_cid = s.add_face_cluster(run_id=run, cluster_no=-1, size=1, label_auto="noise", label_user=None)
    f = _add_face(s, img_id, bbox=[2, 2, 30, 30])
    s.assign_face_to_cluster(f, noise_cid, 0.0)
    s.close()
    r = client.post(f"/api/faces/{f}/name", json={"name": "Carlos"})
    assert r.status_code == 200, r.text
    s = Store(seeded_db)
    # Noise size decremented; face is no longer assigned to noise.
    noise = s.get_face_cluster(noise_cid)
    assert noise is not None
    assert noise["size"] == 0
    rows = s.conn.execute("SELECT cluster_id FROM face_cluster_assignments WHERE face_id=?", (f,)).fetchall()
    cluster_ids = {int(r["cluster_id"]) for r in rows}
    assert noise_cid not in cluster_ids
    s.close()


def test_unidentified_images_excludes_named(client: TestClient, seeded_db: Path) -> None:
    """`/api/faces/unidentified/images` returns photos that still have at least
    one unidentified face. The seeded /tmp/a.jpg has a named face — it must
    not appear unless we add an unidentified face on top."""
    s = Store(seeded_db)
    img_a = _image_id(s, "/tmp/a.jpg")
    s.close()
    r = client.get("/api/faces/unidentified/images")
    assert r.status_code == 200
    paths = {x["path"] for x in r.json()}
    assert "/tmp/a.jpg" not in paths
    # Add an unclustered face on /tmp/a.jpg → now it shows up.
    s = Store(seeded_db)
    _add_face(s, img_a, bbox=[80, 80, 30, 30])
    s.close()
    r = client.get("/api/faces/unidentified/images")
    paths = {x["path"] for x in r.json()}
    assert "/tmp/a.jpg" in paths


def test_triage_queue(client: TestClient, seeded_db: Path) -> None:
    """`/api/faces/triage` lists photos with at least one unverified named
    face OR ≥2 same-name faces on the photo.

    The seeded /tmp/a.jpg has a named, *not-yet-verified* face → it appears.
    A second photo with two faces sharing the same name → also appears, with
    `n_dups` ≥ 1. A fully-validated photo with a single named face stays out.
    """
    s = Store(seeded_db)
    img_b = _image_id(s, "/tmp/b.jpg")
    run = s.latest_face_run()
    assert run is not None
    # img_b: two faces sharing the same label_user → dup-name on this image.
    cid_dup = s.add_face_cluster(run_id=run, cluster_no=20, size=2, label_auto=None, label_user="Carla")
    f_b1 = _add_face(s, img_b, bbox=[0, 0, 30, 30])
    f_b2 = _add_face(s, img_b, bbox=[40, 40, 30, 30])
    s.assign_face_to_cluster(f_b1, cid_dup, 0.0)
    s.assign_face_to_cluster(f_b2, cid_dup, 0.0)
    # A third photo, single named + verified face → must NOT appear.
    img_c = s.upsert_image(
        path="/tmp/c.jpg",
        hash_="h3",
        mtime=3.0,
        width=10,
        height=10,
        exif=None,
        processed_at=_now(),
    )
    cid_ok = s.add_face_cluster(run_id=run, cluster_no=21, size=1, label_auto=None, label_user="Daniel")
    f_c = _add_face(s, img_c, bbox=[5, 5, 30, 30])
    s.assign_face_to_cluster(f_c, cid_ok, 0.0)
    s.set_face_user_verified(f_c, 1)
    s.close()

    r = client.get("/api/faces/triage")
    assert r.status_code == 200
    by_path = {x["path"]: x for x in r.json()}
    assert "/tmp/a.jpg" in by_path  # unverified named face
    assert "/tmp/b.jpg" in by_path  # duplicate-name on the image
    assert "/tmp/c.jpg" not in by_path  # already validated, not a dup
    assert by_path["/tmp/b.jpg"]["n_dups"] >= 1
    # Score: img_b carries the dup → must outrank a clean unverified-only row.
    assert by_path["/tmp/b.jpg"]["score"] > by_path["/tmp/a.jpg"]["score"]
    # Default sort puts highest-score first.
    rows = r.json()
    assert rows == sorted(rows, key=lambda x: (-x["score"], -x["n_unverified"], x["id"]))
    assert {"id", "path", "n_unverified", "n_dups", "score"} <= rows[0].keys()


def test_unidentified_summary(client: TestClient, seeded_db: Path) -> None:
    s = Store(seeded_db)
    img = _image_id(s, "/tmp/b.jpg")
    _add_face(s, img)  # unclustered face → unidentified
    s.close()
    r = client.get("/api/faces/unidentified/summary")
    assert r.status_code == 200
    assert r.json()["unidentified"] >= 1


def test_verify_and_unverify_round_trip(client: TestClient, seeded_db: Path) -> None:
    s = Store(seeded_db)
    img_id = _image_id(s, "/tmp/a.jpg")
    face = s.list_faces_for_image(img_id)[0]["id"]
    s.close()
    r = client.post(f"/api/faces/{face}/verify", json={})
    assert r.status_code == 200
    assert r.json()["user_verified"] == 1
    s = Store(seeded_db)
    row = s.conn.execute("SELECT user_verified FROM faces WHERE id=?", (face,)).fetchone()
    assert row["user_verified"] == 1
    s.close()
    r = client.post(f"/api/faces/{face}/unverify", json={})
    assert r.status_code == 200
    s = Store(seeded_db)
    row = s.conn.execute("SELECT user_verified FROM faces WHERE id=?", (face,)).fetchone()
    assert row["user_verified"] is None
    audit = client.get("/api/faces/corrections?action=unverified").json()
    assert any(c["face_id"] == face for c in audit)
    s.close()


def test_validate_named_bulk(client: TestClient, seeded_db: Path) -> None:
    """All named-but-not-yet-validated faces on an image flip to validated."""
    s = Store(seeded_db)
    img_id = _image_id(s, "/tmp/a.jpg")
    # Seed: img already has 1 named (Anne) face. Add 1 more named, 1 unnamed.
    run = s.latest_face_run()
    assert run is not None
    cid = s.add_face_cluster(run_id=run, cluster_no=10, size=1, label_auto=None, label_user="Bob")
    f_named = _add_face(s, img_id, bbox=[10, 10, 20, 20])
    s.assign_face_to_cluster(f_named, cid, 0.0)
    f_orphan = _add_face(s, img_id, bbox=[40, 40, 20, 20])  # no cluster
    s.close()
    r = client.post(f"/api/images/{img_id}/faces/validate-named", json={})
    assert r.status_code == 200
    assert r.json()["validated"] == 2  # the seeded Anne + the new Bob
    s = Store(seeded_db)
    row = s.conn.execute("SELECT user_verified FROM faces WHERE id=?", (f_named,)).fetchone()
    assert row["user_verified"] == 1
    # orphan stays untouched
    row = s.conn.execute("SELECT user_verified FROM faces WHERE id=?", (f_orphan,)).fetchone()
    assert row["user_verified"] is None
    s.close()


def test_drop_dups_preserves_validated_others(client: TestClient, seeded_db: Path) -> None:
    """Drop-dups must spare other user_verified faces, not just keep_face_id."""
    s = Store(seeded_db)
    run = s.latest_face_run()
    assert run is not None
    img_id = _image_id(s, "/tmp/a.jpg")
    cid = s.add_face_cluster(run_id=run, cluster_no=20, size=3, label_auto=None, label_user="Anne")
    f2 = _add_face(s, img_id, bbox=[60, 60, 30, 30])
    f3 = _add_face(s, img_id, bbox=[100, 100, 30, 30])  # validated dup (montage)
    s.assign_face_to_cluster(f2, cid, 0.0)
    s.assign_face_to_cluster(f3, cid, 0.0)
    s.set_face_user_verified(f3, 1)
    s.close()
    r = client.delete(f"/api/images/{img_id}/faces/dups-of/Anne?keep_face_id={f2}")
    assert r.status_code == 200
    s = Store(seeded_db)
    surviving = {f["id"] for f in s.list_faces_for_image(img_id) if f.get("label_user") == "Anne"}
    assert f2 in surviving
    assert f3 in surviving  # was protected by user_verified=1
    s.close()


def test_lib_wide_unidentified_delete_requires_yes(client: TestClient) -> None:
    r = client.delete("/api/faces/unidentified")
    assert r.status_code == 400
    r = client.delete("/api/faces/unidentified?yes=true")
    assert r.status_code == 200


def test_rename_clusters_skips_noise(client: TestClient, seeded_db: Path) -> None:
    """Bulk rename via /api/people/by-name/{name}/rename must not re-label
    a noise cluster even if it currently shares the name."""
    s = Store(seeded_db)
    run = s.latest_face_run()
    assert run is not None
    noise = s.add_face_cluster(run_id=run, cluster_no=-1, size=1, label_auto="noise", label_user=None)
    # Force a stale label on the noise row (legacy data shape).
    s.conn.execute("UPDATE face_clusters SET label_user='Anne' WHERE id=?", (noise,))
    s.close()
    r = client.post("/api/people/by-name/Anne/rename", json={"name": "Lee"})
    assert r.status_code == 200
    s = Store(seeded_db)
    row = s.conn.execute("SELECT label_user FROM face_clusters WHERE id=?", (noise,)).fetchone()
    assert row["label_user"] == "Anne"  # noise was NOT renamed
    s.close()


def test_api_token_blocks_unauth(seeded_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When APP_API_TOKEN is set, non-public endpoints return 401 without a token."""
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    app = create_app(db_path=seeded_db)
    with TestClient(app) as c:
        # public endpoints stay open
        assert c.get("/healthz").status_code == 200
        # protected endpoints require the token
        assert c.get("/api/runs").status_code == 401
        assert c.get("/api/runs", headers={"X-API-Token": "s3cret"}).status_code == 200
        assert c.get("/api/runs?token=s3cret").status_code == 200
        assert c.get("/api/runs?token=wrong").status_code == 401


def test_api_token_file_hot_rotation(
    seeded_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing APP_API_TOKEN_FILE rotates the accepted token without restart."""
    token_file = tmp_path / "token"
    token_file.write_text("old-token\n")
    monkeypatch.setenv("APP_API_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    app = create_app(db_path=seeded_db)
    with TestClient(app) as c:
        # old token works
        assert c.get("/api/runs", headers={"X-API-Token": "old-token"}).status_code == 200
        assert c.get("/api/runs", headers={"X-API-Token": "new-token"}).status_code == 401
        # rotate the file in-place; same process, no restart
        token_file.write_text("new-token\n")
        assert c.get("/api/runs", headers={"X-API-Token": "old-token"}).status_code == 401
        assert c.get("/api/runs", headers={"X-API-Token": "new-token"}).status_code == 200


def test_api_token_file_empty_returns_503(
    seeded_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty token file with no APP_API_TOKEN fallback must 503, not allow through."""
    token_file = tmp_path / "token"
    token_file.write_text("")
    monkeypatch.setenv("APP_API_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    app = create_app(db_path=seeded_db)
    with TestClient(app) as c:
        # public endpoints still open
        assert c.get("/healthz").status_code == 200
        # protected endpoints refuse with 503 (misconfigured) regardless of token
        assert c.get("/api/runs").status_code == 503
        assert c.get("/api/runs", headers={"X-API-Token": "anything"}).status_code == 503


def test_face_suggest_ranks_known_identity(client: TestClient, seeded_db: Path) -> None:
    """`/api/faces/{id}/suggest` returns top-K identity matches by cosine.

    Seed an Anne identity centroid; insert a face whose embedding sits close
    to that centroid; expect Anne first with sim > 0.5.
    """
    s = Store(seeded_db)
    # Seed an identity centroid pointing along axis 0.
    centroid = np.zeros(512, dtype=np.float32)
    centroid[0] = 1.0
    s.upsert_face_identity("Anne", centroid, n_samples=5)
    # Decoy identity in an orthogonal direction so Anne should clearly win.
    other = np.zeros(512, dtype=np.float32)
    other[1] = 1.0
    s.upsert_face_identity("Bob", other, n_samples=3)

    img_id = _image_id(s, "/tmp/b.jpg")
    # Embedding mostly along axis 0 with a tiny perturbation → cosine close to 1
    # vs Anne, near 0 vs Bob.
    emb = np.zeros(512, dtype=np.float32)
    emb[0] = 0.95
    emb[2] = 0.31  # keep some noise so sim isn't exactly 1.0
    new_face = s.insert_face(
        image_id=img_id,
        bbox=[2, 2, 20, 20],
        det_score=0.9,
        embedding=emb,
        model_name="m",
    )
    s.close()

    r = client.get(f"/api/faces/{new_face}/suggest?k=3")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body, "expected non-empty suggestions"
    assert body[0]["name"] == "Anne"
    assert body[0]["sim"] > 0.5
    assert body[0]["n_samples"] == 5
    # Sorted desc by sim.
    sims = [s["sim"] for s in body]
    assert sims == sorted(sims, reverse=True)


def test_face_suggest_unknown_face_returns_404(client: TestClient) -> None:
    r = client.get("/api/faces/999999/suggest")
    assert r.status_code == 404


def test_face_suggest_no_identities_returns_empty(client: TestClient, seeded_db: Path) -> None:
    s = Store(seeded_db)
    img_id = _image_id(s, "/tmp/a.jpg")
    fid = s.insert_face(
        image_id=img_id,
        bbox=[3, 3, 10, 10],
        det_score=0.9,
        embedding=np.ones(512, dtype=np.float32),
        model_name="m",
    )
    s.close()
    r = client.get(f"/api/faces/{fid}/suggest")
    assert r.status_code == 200
    assert r.json() == []


def test_corrections_logged_on_manual_name(client: TestClient, seeded_db: Path) -> None:
    s = Store(seeded_db)
    img_id = _image_id(s, "/tmp/b.jpg")
    new_face = s.insert_face(
        image_id=img_id,
        bbox=[5, 5, 25, 25],
        det_score=0.9,
        embedding=np.array([0.3] * 512, dtype=np.float32),
        model_name="m",
    )
    s.close()
    r = client.post(f"/api/faces/{new_face}/name", json={"name": "Carla"})
    assert r.status_code == 200
    audit = client.get("/api/faces/corrections?action=named").json()
    assert any(c["face_id"] == new_face and c["name"] == "Carla" for c in audit)


def test_face_identities_merge(client: TestClient, seeded_db: Path) -> None:
    """Merge collapses two `face_identities` into one: blended centroid,
    summed n_samples, every cluster of the loser re-labelled to survivor,
    loser identity row deleted."""
    s = Store(seeded_db)
    run = s.latest_face_run()
    assert run is not None
    # Anne identity along axis 0 with 10 samples.
    c_anne = np.zeros(512, dtype=np.float32)
    c_anne[0] = 1.0
    s.upsert_face_identity("Anne", c_anne, n_samples=10)
    # Lee identity nearly aligned with Anne (the duplicate), 5 samples.
    c_annie = np.zeros(512, dtype=np.float32)
    c_annie[0] = 0.9
    c_annie[1] = 0.1
    s.upsert_face_identity("Lee", c_annie, n_samples=5)
    # Add a real cluster labelled "Lee" so the rename has work to do.
    img_id = _image_id(s, "/tmp/b.jpg")
    cid = s.add_face_cluster(run_id=run, cluster_no=77, size=1, label_auto=None, label_user="Lee")
    f = s.insert_face(
        image_id=img_id,
        bbox=[2, 2, 22, 22],
        det_score=0.9,
        embedding=np.array([0.1] * 512, dtype=np.float32),
        model_name="m",
    )
    s.assign_face_to_cluster(f, cid, 0.0)
    s.close()

    r = client.post("/api/face-identities/merge", json={"survivor": "Anne", "loser": "Lee"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["survivor"] == "Anne"
    assert body["loser"] == "Lee"
    assert body["renamed_clusters"] == 1
    assert body["n_samples"] == 15

    s = Store(seeded_db)
    try:
        names = {i["name"] for i in s.list_face_identities()}
        assert "Anne" in names
        assert "Lee" not in names
        anne_row = next(i for i in s.list_face_identities() if i["name"] == "Anne")
        assert anne_row["n_samples"] == 15
        # Every cluster previously labelled "Lee" now reads "Anne".
        assert s.list_clusters_by_label("Lee") == []
        assert any(c["id"] == cid for c in s.list_clusters_by_label("Anne"))
    finally:
        s.close()


def test_face_identities_merge_missing_loser_404(client: TestClient, seeded_db: Path) -> None:
    s = Store(seeded_db)
    centroid = np.zeros(512, dtype=np.float32)
    centroid[0] = 1.0
    s.upsert_face_identity("Anne", centroid, n_samples=5)
    s.close()
    r = client.post("/api/face-identities/merge", json={"survivor": "Anne", "loser": "Ghost"})
    assert r.status_code == 404


def test_face_identities_merge_same_name_400(client: TestClient, seeded_db: Path) -> None:
    s = Store(seeded_db)
    centroid = np.zeros(512, dtype=np.float32)
    centroid[0] = 1.0
    s.upsert_face_identity("Anne", centroid, n_samples=5)
    s.close()
    r = client.post("/api/face-identities/merge", json={"survivor": "Anne", "loser": "Anne"})
    assert r.status_code == 400


# ---- categories (#23 UI half) ------------------------------------------


def test_categories_crud_and_rule_round_trip(client: TestClient, seeded_db: Path) -> None:
    """Add → list → bind tag → detail shows rule → unbind → delete."""
    # `seeded_db` ships with at least the tag `cat`; we'll bind to it.
    r = client.post("/api/categories", json={"name": "medical"})
    assert r.status_code == 201, r.text
    assert r.json() == {"id": 1, "name": "medical"}

    # Empty-name rejected.
    r = client.post("/api/categories", json={"name": "  "})
    assert r.status_code == 400

    # List shows the new category with zero rules.
    r = client.get("/api/categories")
    assert r.status_code == 200
    assert r.json() == [{"id": 1, "name": "medical", "n_tag_rules": 0, "n_cluster_rules": 0}]

    # Bind an existing tag.
    r = client.post("/api/categories/medical/rules/tag", json={"tag": "cat"})
    assert r.status_code == 200
    assert r.json() == {"category": "medical", "tag": "cat"}

    # Detail surfaces the bound rule.
    body = client.get("/api/categories/medical").json()
    assert body["name"] == "medical"
    assert body["tag_rules"] == [{"tag": "cat", "category": "medical"}]
    assert body["cluster_rules"] == []

    # List counts updated.
    assert client.get("/api/categories").json()[0]["n_tag_rules"] == 1

    # Unbind the rule.
    r = client.delete("/api/categories/rules/tag/cat")
    assert r.status_code == 200
    assert r.json() == {"removed": 1, "tag": "cat"}

    # Delete the category.
    r = client.delete("/api/categories/medical")
    assert r.status_code == 200
    assert r.json() == {"deleted": 1, "name": "medical"}

    # Subsequent delete returns 404.
    r = client.delete("/api/categories/medical")
    assert r.status_code == 404


def test_categories_bind_unknown_tag_returns_404(client: TestClient) -> None:
    """Binding an unknown tag exits 404 with the tag name in the detail."""
    r = client.post("/api/categories", json={"name": "medical"})
    assert r.status_code == 201
    r = client.post("/api/categories/medical/rules/tag", json={"tag": "ghost-tag"})
    assert r.status_code == 404
    assert "ghost-tag" in r.json()["detail"]


def test_categories_detail_404_for_unknown(client: TestClient) -> None:
    r = client.get("/api/categories/ghost-cat")
    assert r.status_code == 404


def test_categories_bind_cluster_round_trip(client: TestClient, seeded_db: Path) -> None:
    """Cluster rule round-trip: bind a real face_cluster id, see it surface,
    unbind it via the dedicated endpoint."""
    s = Store(seeded_db)
    run = s.latest_face_run()
    assert run is not None
    cid = s.add_face_cluster(run_id=run, cluster_no=42, size=1, label_user="Anne", label_auto="Anne")
    s.close()

    r = client.post("/api/categories", json={"name": "family"})
    assert r.status_code == 201
    r = client.post("/api/categories/family/rules/cluster", json={"cluster_id": cid})
    assert r.status_code == 200
    assert r.json() == {"category": "family", "cluster_id": cid}

    detail = client.get("/api/categories/family").json()
    assert len(detail["cluster_rules"]) == 1
    assert detail["cluster_rules"][0]["cluster_id"] == cid
    assert detail["cluster_rules"][0]["label_user"] == "Anne"

    r = client.delete(f"/api/categories/rules/cluster/{cid}")
    assert r.status_code == 200
    assert r.json() == {"removed": 1, "cluster_id": cid}


def test_categories_bind_unknown_cluster_returns_404(client: TestClient) -> None:
    r = client.post("/api/categories", json={"name": "family"})
    assert r.status_code == 201
    r = client.post("/api/categories/family/rules/cluster", json={"cluster_id": 99999})
    assert r.status_code == 404
