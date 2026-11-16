"""Vector similarity interface.

MVP implementation (phase 2): embeddings stored as BLOBs in SQLite and compared
in memory with numpy cosine similarity.  Later: pgvector.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol

from data_navigator.search.text_index import SearchHit


class VectorIndex(Protocol):
    def upsert(self, doc_id: str, vector: Sequence[float]) -> None: ...

    def search(self, vector: Sequence[float], *, limit: int = 10) -> list[SearchHit]: ...

    def remove(self, doc_ids: Iterable[str]) -> None: ...
