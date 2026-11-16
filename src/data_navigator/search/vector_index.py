"""Vector similarity interface.

MVP implementation: embeddings stored as BLOBs in SQLite and compared in memory
with numpy cosine similarity (``sqlite_vector_index``).  Later: pgvector.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol

from data_navigator.search.text_index import SearchHit


class VectorIndex(Protocol):
    def upsert(self, doc_id: str, vector: Sequence[float]) -> None: ...

    def search(
        self, vector: Sequence[float], *, limit: int = 10, prefix: str | None = None
    ) -> list[SearchHit]: ...

    def remove(self, doc_ids: Iterable[str]) -> None: ...

    def clear(self) -> None: ...
