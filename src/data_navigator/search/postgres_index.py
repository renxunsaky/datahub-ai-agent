"""PostgreSQL implementations (not implemented yet).

TODO: ``PostgresTextIndex`` with ``tsvector``/``tsquery`` and
``PostgresVectorIndex`` with pgvector, both exposing the same protocols.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from data_navigator.search.text_index import SearchHit, TextDocument

_MESSAGE = "PostgreSQL search is not implemented yet; use storage.backend: sqlite"


class PostgresTextIndex:
    def __init__(self, url: str) -> None:
        raise NotImplementedError(_MESSAGE)

    def index(self, documents: Iterable[TextDocument]) -> None:  # pragma: no cover - stub
        raise NotImplementedError(_MESSAGE)

    def search(
        self, query: str, *, limit: int = 10, prefix: str | None = None
    ) -> list[SearchHit]:  # pragma: no cover - stub
        raise NotImplementedError(_MESSAGE)

    def remove(self, doc_ids: Iterable[str]) -> None:  # pragma: no cover - stub
        raise NotImplementedError(_MESSAGE)

    def clear(self) -> None:  # pragma: no cover - stub
        raise NotImplementedError(_MESSAGE)


class PostgresVectorIndex:
    def __init__(self, url: str) -> None:
        raise NotImplementedError(_MESSAGE)

    def upsert(self, doc_id: str, vector: Sequence[float]) -> None:  # pragma: no cover - stub
        raise NotImplementedError(_MESSAGE)

    def search(
        self, vector: Sequence[float], *, limit: int = 10, prefix: str | None = None
    ) -> list[SearchHit]:  # pragma: no cover - stub
        raise NotImplementedError(_MESSAGE)

    def remove(self, doc_ids: Iterable[str]) -> None:  # pragma: no cover - stub
        raise NotImplementedError(_MESSAGE)

    def clear(self) -> None:  # pragma: no cover - stub
        raise NotImplementedError(_MESSAGE)
