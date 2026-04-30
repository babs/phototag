import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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
]


@dataclass(frozen=True)
class ImageRow:
    id: int
    path: str
    hash: str
    mtime: float
    width: int | None
    height: int | None


class Store:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because the FastAPI UI runs sync endpoints in
        # a threadpool. Concurrent reads under WAL are safe; writes happen in
        # short transactions and we don't multi-write.
        self.conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        self.conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        current = int(row["value"]) if row else 0
        for idx, sql in enumerate(MIGRATIONS, start=1):
            if idx > current:
                self.conn.executescript(sql)
                self.conn.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(idx),),
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
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

    def get_or_create_tag(self, name: str) -> int:
        row = self.conn.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
        if row:
            return int(row["id"])
        cur = self.conn.execute("INSERT INTO tags(name) VALUES(?) RETURNING id", (name,))
        return int(cur.fetchone()["id"])

    def replace_image_tags(self, image_id: int, model_name: str, tags: Iterable[tuple[str, float]]) -> None:
        self.conn.execute(
            "DELETE FROM image_tags WHERE image_id=? AND model_name=?",
            (image_id, model_name),
        )
        rows: list[tuple[int, int, float, str]] = []
        for name, score in tags:
            rows.append((image_id, self.get_or_create_tag(name), float(score), model_name))
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

    def list_tag_names(self, *, prefix: str | None = None, limit: int = 50) -> list[tuple[str, int]]:
        if prefix:
            cur = self.conn.execute(
                """
                SELECT t.name, COUNT(*) AS n
                FROM tags t JOIN image_tags it ON it.tag_id = t.id
                WHERE t.name LIKE ?
                GROUP BY t.name ORDER BY n DESC LIMIT ?
                """,
                (f"{prefix}%", limit),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT t.name, COUNT(*) AS n
                FROM tags t JOIN image_tags it ON it.tag_id = t.id
                GROUP BY t.name ORDER BY n DESC LIMIT ?
                """,
                (limit,),
            )
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
