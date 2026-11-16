"""SQLite FTS5 implementation of :class:`TextIndex`.

The virtual table lives in the catalog database next to the ORM tables.  It is
not managed by Alembic (virtual tables are SQLite-specific) and is created on
first use.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from sqlalchemy import Engine

from data_navigator.search.text_index import SearchHit, TextDocument

TABLE_NAME = "dn_text_index"
_TOKEN = re.compile(r"[0-9A-Za-z一-鿿]+")


def fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 ``MATCH`` expression (``"a" OR "b"*``)."""
    tokens = [t.lower() for t in _TOKEN.findall(text)]
    tokens = [t for t in tokens if len(t) > 1 or t.isdigit()]
    if not tokens:
        return None
    unique = list(dict.fromkeys(tokens))[:32]
    return " OR ".join(f'"{token}" OR "{token}"*' for token in unique)


class SqliteTextIndex:
    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "sqlite":
            msg = "SqliteTextIndex requires a SQLite engine"
            raise ValueError(msg)
        self._engine = engine
        self._ensure()

    def _ensure(self) -> None:
        with self._raw() as connection:
            connection.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE_NAME} "
                "USING fts5(doc_id UNINDEXED, title, body, tokenize = 'unicode61')"
            )
            connection.commit()

    def _raw(self) -> Any:
        return self._engine.raw_connection()

    def index(self, documents: Iterable[TextDocument]) -> None:
        with self._raw() as connection:
            for document in documents:
                connection.execute(f"DELETE FROM {TABLE_NAME} WHERE doc_id = ?", (document.doc_id,))
                connection.execute(
                    f"INSERT INTO {TABLE_NAME}(doc_id, title, body) VALUES (?, ?, ?)",
                    (document.doc_id, document.title, document.body),
                )
            connection.commit()

    def search(self, query: str, *, limit: int = 10, prefix: str | None = None) -> list[SearchHit]:
        match = fts_query(query)
        if match is None or limit < 1:
            return []
        sql = (
            f"SELECT doc_id, bm25({TABLE_NAME}, 0.0, 2.0, 1.0) AS rank FROM {TABLE_NAME} "
            f"WHERE {TABLE_NAME} MATCH ?"
        )
        params: list[Any] = [match]
        if prefix:
            sql += " AND doc_id LIKE ? ESCAPE '\\'"
            params.append(_like_prefix(prefix))
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        with self._raw() as connection:
            rows = connection.execute(sql, params).fetchall()
        # bm25 returns lower-is-better negative numbers; flip the sign for "higher is better".
        return [SearchHit(doc_id=str(row[0]), score=-float(row[1])) for row in rows]

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
