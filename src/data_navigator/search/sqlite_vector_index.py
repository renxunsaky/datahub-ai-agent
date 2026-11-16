"""SQLite + numpy implementation of :class:`VectorIndex`.

Vectors are stored as little-endian ``float32`` BLOBs in a plain table of the
catalog database and compared in memory with cosine similarity.  Good enough
for a few thousand documents; pgvector replaces it later.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np
from sqlalchemy import Engine

from data_navigator.search.text_index import SearchHit

TABLE_NAME = "dn_vector_index"


class SqliteVectorIndex:
    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "sqlite":
            msg = "SqliteVectorIndex requires a SQLite engine"
            raise ValueError(msg)
        self._engine = engine
        self._ensure()

    def _ensure(self) -> None:
        with self._raw() as connection:
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {TABLE_NAME} "
                "(doc_id TEXT PRIMARY KEY, dim INTEGER NOT NULL, vector BLOB NOT NULL)"
            )
            connection.commit()

    def _raw(self) -> Any:
        return self._engine.raw_connection()

    def upsert(self, doc_id: str, vector: Sequence[float]) -> None:
        array = np.asarray(vector, dtype="<f4")
        if array.ndim != 1 or array.size == 0:
            msg = "vector must be a non-empty 1-D sequence"
            raise ValueError(msg)
        with self._raw() as connection:
            connection.execute(
                f"INSERT INTO {TABLE_NAME}(doc_id, dim, vector) VALUES (?, ?, ?) "
                "ON CONFLICT(doc_id) DO UPDATE SET dim = excluded.dim, vector = excluded.vector",
                (doc_id, int(array.size), array.tobytes()),
            )
            connection.commit()

    def search(
        self, vector: Sequence[float], *, limit: int = 10, prefix: str | None = None
    ) -> list[SearchHit]:
        if limit < 1:
            return []
        query = np.asarray(vector, dtype="<f4")
        norm = float(np.linalg.norm(query))
        if query.ndim != 1 or norm == 0.0:
            return []
        sql = f"SELECT doc_id, dim, vector FROM {TABLE_NAME}"
        params: list[Any] = []
        if prefix:
            sql += " WHERE doc_id LIKE ? ESCAPE '\\'"
            params.append(_like_prefix(prefix))
        with self._raw() as connection:
            rows = connection.execute(sql, params).fetchall()
        ids: list[str] = []
        vectors: list[np.ndarray[Any, Any]] = []
        for doc_id, dim, blob in rows:
            if int(dim) != query.size:
                continue
            ids.append(str(doc_id))
            vectors.append(np.frombuffer(blob, dtype="<f4"))
        if not ids:
            return []
        matrix = np.vstack(vectors)
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0.0] = 1.0
        scores = (matrix @ query) / (norms * norm)
        order = np.argsort(-scores)[:limit]
        return [SearchHit(doc_id=ids[i], score=float(scores[i])) for i in order]

    def remove(self, doc_ids: Iterable[str]) -> None:
        with self._raw() as connection:
            for doc_id in doc_ids:
                connection.execute(f"DELETE FROM {TABLE_NAME} WHERE doc_id = ?", (doc_id,))
            connection.commit()

    def clear(self) -> None:
        with self._raw() as connection:
            connection.execute(f"DELETE FROM {TABLE_NAME}")
            connection.commit()

    def count(self) -> int:
        with self._raw() as connection:
            row = connection.execute(f"SELECT count(*) FROM {TABLE_NAME}").fetchone()
        return int(row[0]) if row else 0


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"
