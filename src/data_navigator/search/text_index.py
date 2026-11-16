"""Full-text search interface.

MVP implementation (phase 2): SQLite FTS5.  Later: PostgreSQL full-text search.
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

    def search(self, query: str, *, limit: int = 10) -> list[SearchHit]: ...

    def remove(self, doc_ids: Iterable[str]) -> None: ...
