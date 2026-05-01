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


def test_runs_empty_no_image_clusters(client: TestClient) -> None:
    # No image clusters seeded → empty list.
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
