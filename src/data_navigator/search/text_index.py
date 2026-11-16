"""Full-text search interface.

MVP implementation: SQLite FTS5 (``sqlite_text_index``).  Later: PostgreSQL
full-text search (``postgres_index`` stub).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TextDocument:
    doc_id: str
    title: str
    body: str


@dataclass(frozen=True)
class SearchHit:
    doc_id: str
    score: float


class TextIndex(Protocol):
    def index(self, documents: Iterable[TextDocument]) -> None: ...

    def search(self, query: str, *, limit: int = 10, prefix: str | None = None) -> list[SearchHit]:
        """Rank documents for *query*; *prefix* restricts to ``doc_id`` values starting with it."""

    def remove(self, doc_ids: Iterable[str]) -> None: ...

    def clear(self) -> None: ...
