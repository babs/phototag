import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime as _dt
from pathlib import Path
from typing import Any

import numpy as np

MIGRATIONS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS images (
        id           INTEGER PRIMARY KEY,
        path         TEXT NOT NULL UNIQUE,
        hash         TEXT NOT NULL,
        mtime        REAL NOT NULL,
        width        INTEGER,
        height       INTEGER,
        exif_json    TEXT,
        processed_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_images_hash ON images(hash);

    CREATE TABLE IF NOT EXISTS tags (
        id   INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE
    );
    CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);

    CREATE TABLE IF NOT EXISTS image_tags (
        image_id   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
        tag_id     INTEGER NOT NULL REFERENCES tags(id),
        score      REAL NOT NULL,
        model_name TEXT NOT NULL,
        PRIMARY KEY (image_id, tag_id, model_name)
    );
    CREATE INDEX IF NOT EXISTS idx_image_tags_score ON image_tags(score);
    CREATE INDEX IF NOT EXISTS idx_image_tags_tag   ON image_tags(tag_id);
    """,
    """
    CREATE TABLE IF NOT EXISTS embeddings (
        image_id   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
        model_name TEXT NOT NULL,
        dim        INTEGER NOT NULL,
        vector     BLOB NOT NULL,
        PRIMARY KEY (image_id, model_name)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS cluster_runs (
        id          INTEGER PRIMARY KEY,
        created_at  TEXT NOT NULL,
        params_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS clusters (
        id         INTEGER PRIMARY KEY,
        run_id     INTEGER NOT NULL REFERENCES cluster_runs(id) ON DELETE CASCADE,
        cluster_no INTEGER NOT NULL,
        label_auto TEXT,
        label_user TEXT,
        size       INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_clusters_run ON clusters(run_id);
    CREATE TABLE IF NOT EXISTS image_clusters (
        image_id   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
        cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
        distance   REAL NOT NULL,
        PRIMARY KEY (image_id, cluster_id)
    );
    CREATE INDEX IF NOT EXISTS idx_image_clusters_cluster ON image_clusters(cluster_id);
    """,
    """
    -- v4: face detection / recognition (opt-in; see specs/15-faces.md)
    CREATE TABLE IF NOT EXISTS faces (
        id           INTEGER PRIMARY KEY,
        image_id     INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
        bbox_json    TEXT NOT NULL,
        det_score    REAL NOT NULL,
        embedding    BLOB NOT NULL,
        dim          INTEGER NOT NULL,
        model_name   TEXT NOT NULL,
        landmarks_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_faces_image ON faces(image_id);

    CREATE TABLE IF NOT EXISTS face_runs (
        id          INTEGER PRIMARY KEY,
        created_at  TEXT NOT NULL,
        params_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS face_clusters (
        id         INTEGER PRIMARY KEY,
        run_id     INTEGER NOT NULL REFERENCES face_runs(id) ON DELETE CASCADE,
        cluster_no INTEGER NOT NULL,
        size       INTEGER NOT NULL,
        label_user TEXT,
        label_auto TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_face_clusters_run ON face_clusters(run_id);

    CREATE TABLE IF NOT EXISTS face_cluster_assignments (
        face_id    INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
        cluster_id INTEGER NOT NULL REFERENCES face_clusters(id) ON DELETE CASCADE,
        distance   REAL NOT NULL,
        PRIMARY KEY (face_id, cluster_id)
    );
    CREATE INDEX IF NOT EXISTS idx_fca_cluster ON face_cluster_assignments(cluster_id);

    CREATE TABLE IF NOT EXISTS face_identities (
        id        INTEGER PRIMARY KEY,
        name      TEXT NOT NULL UNIQUE,
        centroid  BLOB NOT NULL,
        dim       INTEGER NOT NULL,
        n_samples INTEGER NOT NULL
    );
    """,
    """
    -- v5: per-face verification flag (set by `phototag faces verify`).
    -- NULL = not checked yet; 1 = passed; 0 = failed.
    ALTER TABLE faces ADD COLUMN verified INTEGER;
    """,
    """
    -- v6: log every user-driven correction on a face (named, unassigned,
    -- deleted) so a future `phototag faces cluster` pass can use them as
    -- soft constraints (must-link for `named`, cannot-link for `unassigned`).
    -- No FK on face_id/image_id on purpose — the audit trail must survive
    -- deletion of the row it describes.
    CREATE TABLE IF NOT EXISTS face_corrections (
        id          INTEGER PRIMARY KEY,
        face_id     INTEGER,
        image_id    INTEGER,
        action      TEXT NOT NULL,      -- 'named' | 'unassigned' | 'deleted' | 'verified'
        cluster_id  INTEGER,            -- old cluster on 'unassigned'/'named'
        name        TEXT,               -- new label on 'named'
        created_at  TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_face_corrections_face ON face_corrections(face_id);
    CREATE INDEX IF NOT EXISTS idx_face_corrections_action ON face_corrections(action);
    """,
    """
    -- v7: per-face user-confirmed flag (distinct from `verified` which is set
    -- by the heuristic verify pass). user_verified=1 means the user clicked
    -- "verify" on the popover; used to keep this face when "drop other dups
    -- of this name on this image" runs.
    ALTER TABLE faces ADD COLUMN user_verified INTEGER;
    """,
    """
    -- v8: tag.kind separates geo facts ("geo_v1" model) from model
    -- predictions. NULL = legacy/label (RAM/CLIP). 'geo' = reverse-geocoded
    -- city/country. The default search-by-tag scope filters to kind=label so
    -- a city name doesn't drown a visual match with score=1.0.
    ALTER TABLE tags ADD COLUMN kind TEXT;
    UPDATE tags
    SET kind = 'geo'
    WHERE id IN (
        SELECT DISTINCT tag_id FROM image_tags WHERE model_name = 'geo_v1'
    );
    """,
]


def _now_iso_z() -> str:
    return _dt.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ImageRow:
    id: int
    path: str
    hash: str
    mtime: float
    width: int | None
    height: int | None


class Store:
    """SQLite wrapper safe for FastAPI threadpool use.

    Each thread gets its own sqlite3 Connection (sqlite3.Connection objects
    are not safe to share across threads). A process-wide RLock serializes
    write transactions because WAL allows concurrent readers but only one
    writer; without the lock two threads could both call BEGIN on their
    private connections and the second would fail with `database is locked`.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._tls = threading.local()
        # Held across BEGIN..COMMIT to serialize writers.
        self._write_lock = threading.RLock()
        # Touch the connection on the constructing thread so migrations run now.
        self._migrate()

    @property
    def conn(self) -> sqlite3.Connection:
        c: sqlite3.Connection | None = getattr(self._tls, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            # 5 s busy timeout absorbs brief writer contention without raising.
            c.execute("PRAGMA busy_timeout=5000")
            self._tls.conn = c
        return c

    def close(self) -> None:
        c: sqlite3.Connection | None = getattr(self._tls, "conn", None)
        if c is not None:
            c.close()
            self._tls.conn = None

    def _migrate(self) -> None:
        c = self.conn
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        current = int(row["value"]) if row else 0
        for idx, sql in enumerate(MIGRATIONS, start=1):
            if idx <= current:
                continue
            # Wrap schema + version bump in a single executescript so SQLite
            # treats them atomically (executescript runs an implicit COMMIT
            # at script end; if any statement fails the script aborts and the
            # version is not bumped). This avoids the "schema upgraded but
            # version still old" failure mode where re-runs would replay
            # already-applied ALTER TABLE statements.
            wrapped = (
                "BEGIN IMMEDIATE;\n"
                f"{sql}\n"
                "INSERT INTO meta(key,value) VALUES('schema_version', "
                f"'{idx}') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value;\n"
                "COMMIT;\n"
            )
            with self._write_lock:
                try:
                    c.executescript(wrapped)
                except Exception:
                    # executescript already implicitly closed the failing txn;
                    # ROLLBACK is a no-op safety net for partial state.
                    try:
                        c.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass
                    raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            c = self.conn
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    # ---- images ----

    def get_image_by_path(self, path: str) -> ImageRow | None:
        row = self.conn.execute(
            "SELECT id,path,hash,mtime,width,height FROM images WHERE path=?",
            (path,),
        ).fetchone()
        return ImageRow(**dict(row)) if row else None

    def upsert_image(
        self,
        *,
        path: str,
        hash_: str,
        mtime: float,
        width: int | None,
        height: int | None,
        exif: dict[str, Any] | None,
        processed_at: str,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO images(path,hash,mtime,width,height,exif_json,processed_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
                hash=excluded.hash,
                mtime=excluded.mtime,
                width=excluded.width,
                height=excluded.height,
                exif_json=excluded.exif_json,
                processed_at=excluded.processed_at
            RETURNING id
            """,
            (
                path,
                hash_,
                mtime,
                width,
                height,
                json.dumps(exif) if exif else None,
                processed_at,
            ),
        )
        return int(cur.fetchone()["id"])

    def iter_images(self) -> Iterator[ImageRow]:
        cur = self.conn.execute("SELECT id,path,hash,mtime,width,height FROM images ORDER BY id")
        for row in cur:
            yield ImageRow(**dict(row))

    def delete_image(self, image_id: int) -> None:
        """Drop an image row. CASCADE removes tags / clusters / faces tied
        to it. Embeddings on this image are also removed via the FK cascade."""
        self.conn.execute("DELETE FROM images WHERE id=?", (image_id,))

    def update_image_exif(self, image_id: int, exif: dict[str, Any] | None) -> None:
        self.conn.execute(
            "UPDATE images SET exif_json=? WHERE id=?",
            (json.dumps(exif) if exif else None, image_id),
        )

    def get_image_exif(self, image_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT exif_json FROM images WHERE id=?", (image_id,)).fetchone()
        if row is None or not row["exif_json"]:
            return None
        try:
            data = json.loads(row["exif_json"])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None

    def count_images(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM images").fetchone()["n"])

    # ---- tags ----

    def get_or_create_tag(self, name: str, *, kind: str | None = None) -> int:
        # ON CONFLICT branch upgrades the kind if it was previously NULL but
        # never overwrites a non-NULL kind (so a label tag that later coincides
        # with a city name doesn't get demoted to "geo" silently).
        cur = self.conn.execute(
            """
            INSERT INTO tags(name, kind) VALUES(?, ?)
            ON CONFLICT(name) DO UPDATE SET kind = COALESCE(tags.kind, excluded.kind)
            RETURNING id
            """,
            (name, kind),
        )
        return int(cur.fetchone()["id"])

    def replace_image_tags(self, image_id: int, model_name: str, tags: Iterable[tuple[str, float]]) -> None:
        # Tag.kind is derived from the model_name: any model whose name begins
        # with "geo_" produces geo facts; everything else is a label/prediction.
        kind = "geo" if model_name.startswith("geo_") else "label"
        self.conn.execute(
            "DELETE FROM image_tags WHERE image_id=? AND model_name=?",
            (image_id, model_name),
        )
        rows: list[tuple[int, int, float, str]] = []
        for name, score in tags:
            rows.append((image_id, self.get_or_create_tag(name, kind=kind), float(score), model_name))
        if rows:
            self.conn.executemany(
                "INSERT OR IGNORE INTO image_tags(image_id,tag_id,score,model_name) VALUES(?,?,?,?)",
                rows,
            )

    def list_tags_for_image(self, image_id: int) -> list[tuple[str, float, str]]:
        cur = self.conn.execute(
            """
            SELECT t.name, it.score, it.model_name
            FROM image_tags it JOIN tags t ON t.id = it.tag_id
            WHERE it.image_id=? ORDER BY it.score DESC
            """,
            (image_id,),
        )
        return [(r["name"], float(r["score"]), r["model_name"]) for r in cur]

    def tags_per_image(self, image_ids: list[int]) -> dict[int, list[str]]:
        if not image_ids:
            return {}
        placeholders = ",".join("?" * len(image_ids))
        cur = self.conn.execute(
            f"""
            SELECT it.image_id, t.name FROM image_tags it
            JOIN tags t ON t.id = it.tag_id
            WHERE it.image_id IN ({placeholders})
            """,
            image_ids,
        )
        out: dict[int, list[str]] = {i: [] for i in image_ids}
        for row in cur:
            out[int(row["image_id"])].append(row["name"])
        return out

    # ---- embeddings ----

    def upsert_embedding(self, image_id: int, model_name: str, vector: np.ndarray) -> None:
        v = np.ascontiguousarray(vector, dtype=np.float32)
        self.conn.execute(
            """
            INSERT INTO embeddings(image_id,model_name,dim,vector)
            VALUES(?,?,?,?)
            ON CONFLICT(image_id,model_name) DO UPDATE SET
                dim=excluded.dim, vector=excluded.vector
            """,
            (image_id, model_name, int(v.shape[0]), v.tobytes()),
        )

    def has_embedding(self, image_id: int, model_name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM embeddings WHERE image_id=? AND model_name=?",
            (image_id, model_name),
        ).fetchone()
        return row is not None

    def load_embeddings(self, model_name: str) -> tuple[list[int], np.ndarray]:
        cur = self.conn.execute(
            "SELECT image_id, dim, vector FROM embeddings WHERE model_name=? ORDER BY image_id",
            (model_name,),
        )
        ids: list[int] = []
        vectors: list[np.ndarray] = []
        for row in cur:
            ids.append(int(row["image_id"]))
            vectors.append(np.frombuffer(row["vector"], dtype=np.float32, count=int(row["dim"])))
        if not ids:
            return [], np.zeros((0, 0), dtype=np.float32)
        return ids, np.vstack(vectors)

    # ---- clusters ----

    def create_cluster_run(self, params: dict[str, Any], created_at: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO cluster_runs(created_at,params_json) VALUES(?,?) RETURNING id",
            (created_at, json.dumps(params)),
        )
        return int(cur.fetchone()["id"])

    def add_cluster(self, *, run_id: int, cluster_no: int, size: int, label_auto: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO clusters(run_id,cluster_no,label_auto,size) VALUES(?,?,?,?) RETURNING id",
            (run_id, cluster_no, label_auto, size),
        )
        return int(cur.fetchone()["id"])

    def assign_image_to_cluster(self, image_id: int, cluster_id: int, distance: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO image_clusters(image_id,cluster_id,distance) VALUES(?,?,?)",
            (image_id, cluster_id, float(distance)),
        )

    def latest_cluster_run(self) -> int | None:
        row = self.conn.execute("SELECT id FROM cluster_runs ORDER BY id DESC LIMIT 1").fetchone()
        return int(row["id"]) if row else None

    def list_clusters(self, run_id: int) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT id,cluster_no,label_auto,label_user,size FROM clusters WHERE run_id=? ORDER BY size DESC",
            (run_id,),
        )
        return [dict(r) for r in cur]

    def cluster_members(self, cluster_id: int, *, limit: int | None = None) -> list[tuple[int, str, float]]:
        sql = (
            "SELECT i.id, i.path, ic.distance FROM image_clusters ic "
            "JOIN images i ON i.id = ic.image_id WHERE ic.cluster_id=? ORDER BY ic.distance ASC"
        )
        params: list[Any] = [cluster_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        cur = self.conn.execute(sql, params)
        return [(int(r["id"]), r["path"], float(r["distance"])) for r in cur]

    def set_cluster_label_user(self, cluster_id: int, label: str | None) -> None:
        self.conn.execute("UPDATE clusters SET label_user=? WHERE id=?", (label, cluster_id))

    # ---- API helpers (for the UI) ----

    def list_cluster_runs(self) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            """
            SELECT r.id, r.created_at, r.params_json,
                   (SELECT COUNT(*) FROM clusters c WHERE c.run_id=r.id AND c.cluster_no!=-1)
                       AS n_clusters,
                   (SELECT IFNULL(SUM(c.size),0) FROM clusters c WHERE c.run_id=r.id AND c.cluster_no=-1)
                       AS n_noise,
                   (SELECT IFNULL(SUM(c.size),0) FROM clusters c WHERE c.run_id=r.id) AS total_images
            FROM cluster_runs r
            ORDER BY r.id DESC
            """
        )
        return [dict(r) for r in cur]

    def get_image(self, image_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT id, path, hash, mtime, width, height FROM images WHERE id=?",
            (image_id,),
        ).fetchone()
        return dict(row) if row else None

    def cluster_top_tags(self, cluster_id: int, *, top: int = 20) -> list[tuple[str, int]]:
        cur = self.conn.execute(
            """
            SELECT t.name, COUNT(*) AS n
            FROM image_clusters ic
            JOIN image_tags it ON it.image_id = ic.image_id
            JOIN tags t ON t.id = it.tag_id
            WHERE ic.cluster_id=?
            GROUP BY t.name ORDER BY n DESC LIMIT ?
            """,
            (cluster_id, top),
        )
        return [(r["name"], int(r["n"])) for r in cur]

    def search_images_by_tags(
        self,
        tag_names: list[str],
        *,
        min_score: float = 0.0,
        limit: int = 200,
        run_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Images matching ALL tag_names with score >= min_score.

        score = mean over the matched tags' scores. When run_id is given, returns
        one row per image with that run's cluster (or NULLs); without run_id, the
        cluster columns are NULL.
        """
        if not tag_names:
            return []
        placeholders = ",".join("?" * len(tag_names))
        params: list[Any] = list(tag_names) + [float(min_score), len(tag_names)]
        # Subquery the cluster join so the run_id filter scopes the join itself
        # (predicate as JOIN-ON is broken: it leaves duplicate NULL rows).
        if run_id is not None:
            cluster_join = """
                LEFT JOIN (
                    SELECT ic.image_id,
                           c.id AS cluster_id, c.cluster_no, c.label_auto,
                           c.label_user, c.size AS cluster_size
                    FROM image_clusters ic
                    JOIN clusters c ON c.id = ic.cluster_id
                    WHERE c.run_id = ?
                ) cj ON cj.image_id = i.id
            """
            params.insert(len(tag_names) + 2, int(run_id))
            select_cluster = "cj.cluster_id, cj.cluster_no, cj.label_auto, cj.label_user, cj.cluster_size"
        else:
            cluster_join = ""
            select_cluster = (
                "NULL AS cluster_id, NULL AS cluster_no, NULL AS label_auto, "
                "NULL AS label_user, NULL AS cluster_size"
            )
        sql = f"""
            WITH match AS (
                SELECT it.image_id, AVG(it.score) AS score
                FROM image_tags it
                JOIN tags t ON t.id = it.tag_id
                WHERE t.name IN ({placeholders}) AND it.score >= ?
                GROUP BY it.image_id
                HAVING COUNT(DISTINCT t.id) = ?
            )
            SELECT i.id, i.path, m.score, {select_cluster}
            FROM match m
            JOIN images i ON i.id = m.image_id
            {cluster_join}
            ORDER BY m.score DESC LIMIT ?
        """
        params.append(limit)
        cur = self.conn.execute(sql, params)
        return [dict(r) for r in cur]

    def list_tag_names(
        self,
        *,
        prefix: str | None = None,
        limit: int = 50,
        kind: str | None = None,
        include_geo: bool = True,
    ) -> list[tuple[str, int]]:
        """List tag names + counts. By default includes both labels and geo
        facts (back-compat). Pass `kind="label"` for ML predictions only,
        `kind="geo"` for cities/countries only, or `include_geo=False` to
        exclude geo from a label list while keeping NULL-kind legacy rows."""
        clauses = []
        params: list[Any] = []
        if prefix:
            clauses.append("t.name LIKE ?")
            params.append(f"{prefix}%")
        if kind is not None:
            # NULL counts as "label" (legacy rows pre-v8 migration backfill).
            if kind == "label":
                clauses.append("(t.kind IS NULL OR t.kind = 'label')")
            else:
                clauses.append("t.kind = ?")
                params.append(kind)
        elif not include_geo:
            clauses.append("(t.kind IS NULL OR t.kind != 'geo')")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT t.name, COUNT(*) AS n "
            "FROM tags t JOIN image_tags it ON it.tag_id = t.id"
            f"{where} "
            "GROUP BY t.name ORDER BY n DESC LIMIT ?"
        )
        params.append(limit)
        cur = self.conn.execute(sql, params)
        return [(r["name"], int(r["n"])) for r in cur]

    def list_image_tags(self, image_id: int, *, min_score: float = 0.0) -> list[tuple[str, float]]:
        cur = self.conn.execute(
            """
            SELECT t.name, it.score FROM image_tags it
            JOIN tags t ON t.id = it.tag_id
            WHERE it.image_id=? AND it.score >= ?
            ORDER BY it.score DESC
            """,
            (image_id, float(min_score)),
        )
        return [(r["name"], float(r["score"])) for r in cur]

    def get_cluster(self, cluster_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT id, run_id, cluster_no, label_auto, label_user, size FROM clusters WHERE id=?",
            (cluster_id,),
        ).fetchone()
        return dict(row) if row else None

    # ---- faces (v2) ----

    def insert_face(
        self,
        *,
        image_id: int,
        bbox: list[int],
        det_score: float,
        embedding: np.ndarray,
        model_name: str,
        landmarks: list[list[float]] | None = None,
    ) -> int:
        v = np.ascontiguousarray(embedding, dtype=np.float32)
        cur = self.conn.execute(
            """
            INSERT INTO faces(image_id,bbox_json,det_score,embedding,dim,model_name,landmarks_json)
            VALUES(?,?,?,?,?,?,?) RETURNING id
            """,
            (
                image_id,
                json.dumps(bbox),
                float(det_score),
                v.tobytes(),
                int(v.shape[0]),
                model_name,
                json.dumps(landmarks) if landmarks else None,
            ),
        )
        return int(cur.fetchone()["id"])

    def has_faces(self, image_id: int, model_name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM faces WHERE image_id=? AND model_name=? LIMIT 1",
            (image_id, model_name),
        ).fetchone()
        return row is not None

    def delete_faces_for_image(self, image_id: int, model_name: str) -> None:
        self.conn.execute(
            "DELETE FROM faces WHERE image_id=? AND model_name=?",
            (image_id, model_name),
        )

    def load_face_embeddings(self, model_name: str) -> tuple[list[int], np.ndarray]:
        cur = self.conn.execute(
            "SELECT id, dim, embedding FROM faces WHERE model_name=? ORDER BY id",
            (model_name,),
        )
        ids: list[int] = []
        vectors: list[np.ndarray] = []
        for row in cur:
            ids.append(int(row["id"]))
            vectors.append(np.frombuffer(row["embedding"], dtype=np.float32, count=int(row["dim"])))
        if not ids:
            return [], np.zeros((0, 0), dtype=np.float32)
        return ids, np.vstack(vectors)

    def list_faces_for_image(self, image_id: int) -> list[dict[str, Any]]:
        # For faces that exist in multiple face_runs (auto + manual), pick the
        # row from the *most recent run that has an assignment for this face*.
        # Picking globally-latest-run can hide a face that's in an older
        # run but not in a newer one (the common case after a manual run is
        # created on top of an auto-cluster).
        cur = self.conn.execute(
            """
            WITH ranked AS (
                SELECT
                    fca.face_id,
                    fc.id   AS cluster_id,
                    fc.cluster_no,
                    fc.label_user,
                    fc.label_auto,
                    fc.run_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY fca.face_id ORDER BY fc.run_id DESC
                    ) AS rn
                FROM face_cluster_assignments fca
                JOIN face_clusters fc ON fc.id = fca.cluster_id
            )
            SELECT f.id, f.bbox_json, f.det_score, f.verified, f.user_verified,
                   r.cluster_id, r.cluster_no, r.label_user, r.label_auto
            FROM faces f
            LEFT JOIN ranked r ON r.face_id = f.id AND r.rn = 1
            WHERE f.image_id = ?
            """,
            (image_id,),
        )
        out: list[dict[str, Any]] = []
        for row in cur:
            try:
                bbox = json.loads(row["bbox_json"])
            except json.JSONDecodeError:
                bbox = None
            out.append(
                {
                    "id": int(row["id"]),
                    "bbox": bbox,
                    "det_score": float(row["det_score"]),
                    "verified": row["verified"],
                    "user_verified": row["user_verified"],
                    "cluster_id": row["cluster_id"],
                    "cluster_no": row["cluster_no"],
                    "label_user": row["label_user"],
                    "label_auto": row["label_auto"],
                }
            )
        return out

    def get_face(self, face_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT id, image_id, bbox_json, det_score, model_name FROM faces WHERE id=?",
            (face_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["bbox"] = json.loads(d.pop("bbox_json"))
        except json.JSONDecodeError:
            d["bbox"] = None
        return d

    def count_faces(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM faces").fetchone()["n"])

    def faces_summary(self) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS faces, COUNT(DISTINCT image_id) AS images FROM faces"
        ).fetchone()
        return {"faces": int(row["faces"]), "images": int(row["images"])}

    def list_images_with_faces(self, *, limit: int = 300) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            """
            SELECT i.id, i.path, COUNT(f.id) AS face_count
            FROM images i JOIN faces f ON f.image_id = i.id
            GROUP BY i.id
            ORDER BY face_count DESC, i.id
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in cur]

    def create_face_run(self, params: dict[str, Any], created_at: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO face_runs(created_at,params_json) VALUES(?,?) RETURNING id",
            (created_at, json.dumps(params)),
        )
        return int(cur.fetchone()["id"])

    def add_face_cluster(
        self,
        *,
        run_id: int,
        cluster_no: int,
        size: int,
        label_auto: str | None,
        label_user: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO face_clusters(run_id,cluster_no,size,label_auto,label_user) "
            "VALUES(?,?,?,?,?) RETURNING id",
            (run_id, cluster_no, size, label_auto, label_user),
        )
        return int(cur.fetchone()["id"])

    def assign_face_to_cluster(self, face_id: int, cluster_id: int, distance: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO face_cluster_assignments(face_id,cluster_id,distance) VALUES(?,?,?)",
            (face_id, cluster_id, float(distance)),
        )

    def latest_face_run(self) -> int | None:
        row = self.conn.execute("SELECT id FROM face_runs ORDER BY id DESC LIMIT 1").fetchone()
        return int(row["id"]) if row else None

    def list_named_people(self, *, prefix: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """All named-people across face_runs.

        Each entry: {name, count (distinct images), n_clusters (face_clusters
        sharing that label_user)}.
        """
        if prefix:
            cur = self.conn.execute(
                """
                SELECT fc.label_user AS name,
                       COUNT(DISTINCT f.image_id) AS n,
                       COUNT(DISTINCT fc.id) AS n_clusters
                FROM face_clusters fc
                JOIN face_cluster_assignments fca ON fca.cluster_id = fc.id
                JOIN faces f ON f.id = fca.face_id
                WHERE fc.label_user IS NOT NULL AND fc.label_user LIKE ?
                GROUP BY fc.label_user ORDER BY n DESC LIMIT ?
                """,
                (f"{prefix}%", limit),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT fc.label_user AS name,
                       COUNT(DISTINCT f.image_id) AS n,
                       COUNT(DISTINCT fc.id) AS n_clusters
                FROM face_clusters fc
                JOIN face_cluster_assignments fca ON fca.cluster_id = fc.id
                JOIN faces f ON f.id = fca.face_id
                WHERE fc.label_user IS NOT NULL
                GROUP BY fc.label_user ORDER BY n DESC LIMIT ?
                """,
                (limit,),
            )
        return [
            {
                "name": r["name"],
                "count": int(r["n"]),
                "n_clusters": int(r["n_clusters"]),
            }
            for r in cur
        ]

    def list_clusters_by_label(self, label: str) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT id, run_id, cluster_no, size, label_user, label_auto "
            "FROM face_clusters WHERE label_user=? ORDER BY size DESC",
            (label,),
        )
        return [dict(r) for r in cur]

    def rename_clusters_by_label(self, old: str, new: str | None) -> int:
        # Never propagate a label onto the noise cluster (cluster_no=-1).
        # Noise groups visually-unrelated faces and labelling it would tag
        # everyone in the bag with the same name.
        cur = self.conn.execute(
            "UPDATE face_clusters SET label_user=? WHERE label_user=? AND cluster_no != -1",
            (new, old),
        )
        return int(cur.rowcount)

    def cluster_centroid(self, cluster_id: int) -> np.ndarray | None:
        cur = self.conn.execute(
            """
            SELECT f.dim, f.embedding
            FROM face_cluster_assignments fca
            JOIN faces f ON f.id = fca.face_id
            WHERE fca.cluster_id = ?
            """,
            (cluster_id,),
        )
        vecs = [np.frombuffer(row["embedding"], dtype=np.float32, count=int(row["dim"])) for row in cur]
        if not vecs:
            return None
        return np.mean(np.vstack(vecs), axis=0).astype(np.float32)

    def delete_face_identity(self, name: str) -> None:
        self.conn.execute("DELETE FROM face_identities WHERE name=?", (name,))

    def delete_face(self, face_id: int) -> None:
        """Drop the face row entirely. Cascades cluster_assignments."""
        # Decrement cluster sizes first so display stays consistent.
        rows = self.conn.execute(
            "SELECT cluster_id FROM face_cluster_assignments WHERE face_id=?",
            (face_id,),
        ).fetchall()
        for r in rows:
            self.conn.execute(
                "UPDATE face_clusters SET size = MAX(0, size - 1) WHERE id=?",
                (r["cluster_id"],),
            )
        self.conn.execute("DELETE FROM faces WHERE id=?", (face_id,))

    def log_face_correction(
        self,
        *,
        face_id: int | None,
        image_id: int | None,
        action: str,
        cluster_id: int | None = None,
        name: str | None = None,
    ) -> int:
        """Append a row to face_corrections; cheap audit trail of user edits."""
        cur = self.conn.execute(
            """
            INSERT INTO face_corrections(face_id,image_id,action,cluster_id,name,created_at)
            VALUES(?,?,?,?,?,?) RETURNING id
            """,
            (
                face_id,
                image_id,
                action,
                cluster_id,
                name,
                _now_iso_z(),
            ),
        )
        return int(cur.fetchone()["id"])

    def list_face_corrections(
        self, *, face_id: int | None = None, action: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT id, face_id, image_id, action, cluster_id, name, created_at FROM face_corrections"
        params: list[Any] = []
        clauses: list[str] = []
        if face_id is not None:
            clauses.append("face_id=?")
            params.append(face_id)
        if action is not None:
            clauses.append("action=?")
            params.append(action)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        return [dict(r) for r in self.conn.execute(sql, params)]

    def set_face_verified(self, face_id: int, value: int | None) -> None:
        self.conn.execute("UPDATE faces SET verified=? WHERE id=?", (value, face_id))

    def set_face_user_verified(self, face_id: int, value: int | None) -> None:
        """User-driven 'this detection is correct' flag. Distinct from
        `verified` which carries the heuristic verify_faces outcome."""
        self.conn.execute("UPDATE faces SET user_verified=? WHERE id=?", (value, face_id))

    def delete_other_named_faces_on_image(self, image_id: int, label: str, keep_face_id: int) -> int:
        """Drop faces on `image_id` carrying user-label `label`, except
        `keep_face_id` and any other user-validated face.

        Validated faces are protected: a montage / mirror legitimately carries
        the same person twice and the user told us so by validating both. Only
        untrusted duplicates (the auto-clusterer's mistakes) get removed.
        """
        rows = self.conn.execute(
            """
            SELECT DISTINCT f.id AS face_id
            FROM faces f
            JOIN face_cluster_assignments fca ON fca.face_id = f.id
            JOIN face_clusters fc ON fc.id = fca.cluster_id
            WHERE f.image_id = ?
              AND fc.label_user = ?
              AND f.id != ?
              AND (f.user_verified IS NULL OR f.user_verified = 0)
            """,
            (image_id, label, keep_face_id),
        ).fetchall()
        n = 0
        for r in rows:
            self.delete_face(int(r["face_id"]))
            n += 1
        return n

    def iter_faces_for_verify(self) -> Iterator[dict[str, Any]]:
        cur = self.conn.execute("SELECT id, image_id, bbox_json, det_score, verified FROM faces")
        for row in cur:
            d = dict(row)
            try:
                d["bbox"] = json.loads(d.pop("bbox_json"))
            except json.JSONDecodeError:
                d["bbox"] = None
            yield d

    def delete_unidentified_faces_for_image(self, image_id: int) -> int:
        """Drop faces on this image that have no `label_user` (named) assignment.

        Unidentified means: either no cluster row at all, OR every cluster the
        face belongs to has `label_user IS NULL` (the auto "person N" labels).
        """
        rows = self.conn.execute(
            """
            SELECT f.id AS face_id
            FROM faces f
            WHERE f.image_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM face_cluster_assignments fca
                  JOIN face_clusters fc ON fc.id = fca.cluster_id
                  WHERE fca.face_id = f.id AND fc.label_user IS NOT NULL
              )
            """,
            (image_id,),
        ).fetchall()
        n = 0
        for r in rows:
            self.delete_face(int(r["face_id"]))
            n += 1
        return n

    def delete_all_unidentified_faces(self) -> int:
        """Library-wide variant of delete_unidentified_faces_for_image."""
        rows = self.conn.execute(
            """
            SELECT f.id AS face_id
            FROM faces f
            WHERE NOT EXISTS (
                SELECT 1 FROM face_cluster_assignments fca
                JOIN face_clusters fc ON fc.id = fca.cluster_id
                WHERE fca.face_id = f.id AND fc.label_user IS NOT NULL
            )
            """
        ).fetchall()
        n = 0
        for r in rows:
            self.delete_face(int(r["face_id"]))
            n += 1
        return n

    def count_unidentified_faces(self) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM faces f
            WHERE NOT EXISTS (
                SELECT 1 FROM face_cluster_assignments fca
                JOIN face_clusters fc ON fc.id = fca.cluster_id
                WHERE fca.face_id = f.id AND fc.label_user IS NOT NULL
            )
            """
        ).fetchone()
        return int(row["n"])

    def list_unidentified_face_clusters(self, run_id: int) -> list[dict[str, Any]]:
        """Within a run, every cluster lacking a user label (auto 'person N')."""
        cur = self.conn.execute(
            "SELECT id, cluster_no, size, label_auto, label_user FROM face_clusters "
            "WHERE run_id=? AND label_user IS NULL AND cluster_no!=-1 ORDER BY size DESC",
            (run_id,),
        )
        return [dict(r) for r in cur]

    def delete_all_faces_for_image(self, image_id: int) -> int:
        """Drop every face row for the image; bulk-update affected cluster sizes."""
        rows = self.conn.execute(
            """
            SELECT fca.cluster_id AS cid, COUNT(*) AS n
            FROM face_cluster_assignments fca
            JOIN faces f ON f.id = fca.face_id
            WHERE f.image_id = ?
            GROUP BY fca.cluster_id
            """,
            (image_id,),
        ).fetchall()
        for r in rows:
            self.conn.execute(
                "UPDATE face_clusters SET size = MAX(0, size - ?) WHERE id = ?",
                (int(r["n"]), int(r["cid"])),
            )
        cur = self.conn.execute("DELETE FROM faces WHERE image_id=?", (image_id,))
        return int(cur.rowcount)

    def face_clusters_for_face(self, face_id: int) -> list[int]:
        """All cluster ids this face is assigned to (any run)."""
        cur = self.conn.execute(
            "SELECT cluster_id FROM face_cluster_assignments WHERE face_id=?",
            (face_id,),
        )
        return [int(r["cluster_id"]) for r in cur]

    def latest_run_holding_face(self, face_id: int) -> int | None:
        """Most recent face_run that has an assignment for this face, or None.

        This mirrors the run picked by `list_faces_for_image` (display layer)
        so user-facing "unassign" acts on the cluster the user is actually
        looking at, even when the global latest_face_run does not include
        this face (e.g., manual run predates a newer auto run).
        """
        row = self.conn.execute(
            """
            SELECT fc.run_id AS run_id
            FROM face_cluster_assignments fca
            JOIN face_clusters fc ON fc.id = fca.cluster_id
            WHERE fca.face_id = ?
            ORDER BY fc.run_id DESC
            LIMIT 1
            """,
            (face_id,),
        ).fetchone()
        return int(row["run_id"]) if row else None

    def face_clusters_for_face_in_run(self, face_id: int, run_id: int) -> list[int]:
        cur = self.conn.execute(
            """
            SELECT fc.id AS cid FROM face_cluster_assignments fca
            JOIN face_clusters fc ON fc.id = fca.cluster_id
            WHERE fca.face_id=? AND fc.run_id=?
            """,
            (face_id, run_id),
        )
        return [int(r["cid"]) for r in cur]

    def unassign_face_globally(self, face_id: int) -> int:
        cids = self.face_clusters_for_face(face_id)
        if not cids:
            return 0
        placeholders = ",".join("?" * len(cids))
        self.conn.execute(
            f"DELETE FROM face_cluster_assignments WHERE face_id=? AND cluster_id IN ({placeholders})",
            [face_id, *cids],
        )
        for cid in cids:
            self.conn.execute(
                "UPDATE face_clusters SET size = MAX(0, size - 1) WHERE id=?",
                (cid,),
            )
        return len(cids)

    def unassign_face_from_run(self, face_id: int, run_id: int) -> int:
        """Remove this face's cluster assignment(s) within a single run.

        Returns the count of removed rows.
        """
        # Look up cluster ids first to update sizes.
        cur = self.conn.execute(
            """
            SELECT fc.id AS cluster_id
            FROM face_cluster_assignments fca
            JOIN face_clusters fc ON fc.id = fca.cluster_id
            WHERE fca.face_id=? AND fc.run_id=?
            """,
            (face_id, run_id),
        )
        cids = [int(r["cluster_id"]) for r in cur]
        if not cids:
            return 0
        placeholders = ",".join("?" * len(cids))
        self.conn.execute(
            f"DELETE FROM face_cluster_assignments WHERE face_id=? AND cluster_id IN ({placeholders})",
            [face_id, *cids],
        )
        for cid in cids:
            self.conn.execute(
                "UPDATE face_clusters SET size = MAX(0, size - 1) WHERE id=?",
                (cid,),
            )
        return len(cids)

    def search_images_by_persons(self, names: list[str]) -> set[int]:
        """Image ids containing a face in a cluster named for each of `names` (AND)."""
        if not names:
            return set()
        placeholders = ",".join("?" * len(names))
        cur = self.conn.execute(
            f"""
            SELECT f.image_id, fc.label_user
            FROM faces f
            JOIN face_cluster_assignments fca ON fca.face_id = f.id
            JOIN face_clusters fc ON fc.id = fca.cluster_id
            WHERE fc.label_user IN ({placeholders})
            """,
            names,
        )
        by_name: dict[str, set[int]] = {n: set() for n in names}
        for row in cur:
            n = row["label_user"]
            if n in by_name:
                by_name[n].add(int(row["image_id"]))
        common: set[int] | None = None
        for name in names:
            s = by_name.get(name, set())
            common = s if common is None else common & s
        return common or set()

    def validate_named_unvalidated_for_image(self, image_id: int) -> list[int]:
        """Mark every named face on this image as user_verified=1 unless it
        already is. Returns the list of face_ids that flipped, so the caller
        can write per-face audit rows.
        """
        rows = self.conn.execute(
            """
            SELECT f.id FROM faces f
            WHERE f.image_id = ?
              AND (f.user_verified IS NULL OR f.user_verified = 0)
              AND EXISTS (
                  SELECT 1 FROM face_cluster_assignments fca
                  JOIN face_clusters fc ON fc.id = fca.cluster_id
                  WHERE fca.face_id = f.id AND fc.label_user IS NOT NULL
              )
            """,
            (image_id,),
        ).fetchall()
        ids = [int(r["id"]) for r in rows]
        for fid in ids:
            self.conn.execute("UPDATE faces SET user_verified = 1 WHERE id = ?", (fid,))
        return ids

    def count_named_unvalidated_for_image(self, image_id: int) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM faces f
            WHERE f.image_id = ?
              AND (f.user_verified IS NULL OR f.user_verified = 0)
              AND EXISTS (
                  SELECT 1 FROM face_cluster_assignments fca
                  JOIN face_clusters fc ON fc.id = fca.cluster_id
                  WHERE fca.face_id = f.id AND fc.label_user IS NOT NULL
              )
            """,
            (image_id,),
        ).fetchone()
        return int(row["n"])

    def load_orphan_face_embeddings(self, model_name: str) -> tuple[list[int], np.ndarray]:
        """Load embeddings for orphan/noise faces only.

        An orphan face has no `face_cluster_assignments` row whose cluster
        carries a `label_user`. This mirrors `count_unidentified_faces`.
        Used by the noise/orphan re-cluster pass.
        """
        cur = self.conn.execute(
            """
            SELECT f.id, f.dim, f.embedding
            FROM faces f
            WHERE f.model_name = ?
              AND NOT EXISTS (
                  SELECT 1 FROM face_cluster_assignments fca
                  JOIN face_clusters fc ON fc.id = fca.cluster_id
                  WHERE fca.face_id = f.id AND fc.label_user IS NOT NULL
              )
            ORDER BY f.id
            """,
            (model_name,),
        )
        ids: list[int] = []
        vecs: list[np.ndarray] = []
        for row in cur:
            ids.append(int(row["id"]))
            vecs.append(np.frombuffer(row["embedding"], dtype=np.float32, count=int(row["dim"])))
        if not ids:
            return [], np.zeros((0, 0), dtype=np.float32)
        return ids, np.vstack(vecs)

    def list_user_verified_faces_for_image(self, image_id: int) -> list[dict[str, Any]]:
        """Faces on this image that the user has validated. Used by the
        redetect path to keep them across a re-run."""
        cur = self.conn.execute(
            "SELECT id, bbox_json FROM faces WHERE image_id=? AND user_verified=1",
            (image_id,),
        )
        out: list[dict[str, Any]] = []
        for row in cur:
            try:
                bbox = json.loads(row["bbox_json"])
            except json.JSONDecodeError:
                bbox = None
            out.append({"id": int(row["id"]), "bbox": bbox})
        return out

    def delete_non_verified_faces_for_image(self, image_id: int, model_name: str) -> int:
        """Delete every face row for the image *except* user-verified ones.

        Cluster sizes are decremented for each removed face's assignments.
        """
        rows = self.conn.execute(
            "SELECT id FROM faces WHERE image_id=? AND model_name=? "
            "AND (user_verified IS NULL OR user_verified = 0)",
            (image_id, model_name),
        ).fetchall()
        n = 0
        for r in rows:
            self.delete_face(int(r["id"]))
            n += 1
        return n

    def detach_face_from_noise(self, face_id: int) -> int:
        """Drop this face's assignments to any noise cluster (cluster_no=-1).

        Used right after a face is given a real identity: keeping the noise
        membership would over-count the noise cluster size and clutter the
        unidentified view with a now-named face.
        """
        rows = self.conn.execute(
            """
            SELECT fc.id AS cluster_id
            FROM face_cluster_assignments fca
            JOIN face_clusters fc ON fc.id = fca.cluster_id
            WHERE fca.face_id = ? AND fc.cluster_no = -1
            """,
            (face_id,),
        ).fetchall()
        cids = [int(r["cluster_id"]) for r in rows]
        if not cids:
            return 0
        placeholders = ",".join("?" * len(cids))
        self.conn.execute(
            f"DELETE FROM face_cluster_assignments WHERE face_id=? AND cluster_id IN ({placeholders})",
            [face_id, *cids],
        )
        for cid in cids:
            self.conn.execute(
                "UPDATE face_clusters SET size = MAX(0, size - 1) WHERE id=?",
                (cid,),
            )
        return len(cids)

    def list_images_with_unidentified_faces(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Photos containing at least one unidentified face.

        "Unidentified" = same definition as `count_unidentified_faces`: no
        cluster carrying a `label_user`. The count column is the *unidentified*
        face count for that photo, not the total face count.
        """
        cur = self.conn.execute(
            """
            SELECT i.id, i.path, COUNT(f.id) AS face_count
            FROM images i
            JOIN faces f ON f.image_id = i.id
            WHERE NOT EXISTS (
                SELECT 1 FROM face_cluster_assignments fca
                JOIN face_clusters fc ON fc.id = fca.cluster_id
                WHERE fca.face_id = f.id AND fc.label_user IS NOT NULL
            )
            GROUP BY i.id
            ORDER BY face_count DESC, i.id
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in cur]

    def clear_noise_cluster_labels(self) -> int:
        """Wipe any user-label that was mistakenly applied to a noise cluster.

        Noise clusters (cluster_no=-1) group unrelated faces; labelling them
        propagates the name to every unrelated face. Returns the number of
        rows updated. Safe to call repeatedly.

        TODO: this does NOT remove the corresponding `face_identities` row
        whose centroid was blended from noise faces — that centroid is now
        corrupting future identity matching at re-cluster time. Manual fix
        for now: `phototag faces purge --keep-identities=false`.
        """
        cur = self.conn.execute(
            "UPDATE face_clusters SET label_user = NULL WHERE cluster_no = -1 AND label_user IS NOT NULL"
        )
        return int(cur.rowcount)

    def list_face_clusters(self, run_id: int) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT id, cluster_no, size, label_auto, label_user FROM face_clusters "
            "WHERE run_id=? ORDER BY (label_user IS NULL), size DESC",
            (run_id,),
        )
        return [dict(r) for r in cur]

    def get_face_cluster(self, cluster_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT id, run_id, cluster_no, size, label_auto, label_user FROM face_clusters WHERE id=?",
            (cluster_id,),
        ).fetchone()
        return dict(row) if row else None

    def face_cluster_members(self, cluster_id: int, *, limit: int | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT f.id AS face_id, f.image_id, f.bbox_json, f.det_score, fca.distance, i.path
            FROM face_cluster_assignments fca
            JOIN faces f ON f.id = fca.face_id
            JOIN images i ON i.id = f.image_id
            WHERE fca.cluster_id = ?
            ORDER BY fca.distance ASC
        """
        params: list[Any] = [cluster_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        cur = self.conn.execute(sql, params)
        out: list[dict[str, Any]] = []
        for row in cur:
            d = dict(row)
            try:
                d["bbox"] = json.loads(d.pop("bbox_json"))
            except json.JSONDecodeError:
                d["bbox"] = None
            out.append(d)
        return out

    def set_face_cluster_label_user(self, cluster_id: int, label: str | None) -> None:
        self.conn.execute(
            "UPDATE face_clusters SET label_user=? WHERE id=?",
            (label, cluster_id),
        )

    def list_face_identities(self) -> list[dict[str, Any]]:
        cur = self.conn.execute("SELECT id, name, centroid, dim, n_samples FROM face_identities")
        out: list[dict[str, Any]] = []
        for row in cur:
            out.append(
                {
                    "id": int(row["id"]),
                    "name": row["name"],
                    "centroid": np.frombuffer(row["centroid"], dtype=np.float32, count=int(row["dim"])),
                    "dim": int(row["dim"]),
                    "n_samples": int(row["n_samples"]),
                }
            )
        return out

    def upsert_face_identity(self, name: str, centroid: np.ndarray, n_samples: int) -> int:
        v = np.ascontiguousarray(centroid, dtype=np.float32)
        cur = self.conn.execute(
            """
            INSERT INTO face_identities(name,centroid,dim,n_samples) VALUES(?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET
                centroid=excluded.centroid, dim=excluded.dim, n_samples=excluded.n_samples
            RETURNING id
            """,
            (name, v.tobytes(), int(v.shape[0]), int(n_samples)),
        )
        return int(cur.fetchone()["id"])

    def purge_faces(self, *, keep_identities: bool = False) -> None:
        # Spec 15-faces.md: "wipe is total" — corrections audit trail goes too
        # unless --keep-identities is set (in which case corrections referencing
        # surviving identities stay readable for future re-cluster passes).
        self.conn.execute("DELETE FROM face_cluster_assignments")
        self.conn.execute("DELETE FROM face_clusters")
        self.conn.execute("DELETE FROM face_runs")
        self.conn.execute("DELETE FROM faces")
        if not keep_identities:
            self.conn.execute("DELETE FROM face_identities")
            self.conn.execute("DELETE FROM face_corrections")
