"""Hybrid retrieval: full-text and vector hits fused with reciprocal rank fusion.

Without an embedding function (no embedding model configured, or the model
failed) the search silently degrades to full-text only and says so in the log.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from data_navigator.search.text_index import SearchHit, TextDocument, TextIndex
from data_navigator.search.vector_index import VectorIndex

log = logging.getLogger(__name__)

Embedder = Callable[[Sequence[str]], list[list[float]]]


@dataclass
class IndexSummary:
    documents: int = 0
    embedded: int = 0
    embedding_errors: int = 0
    vector_enabled: bool = False

    def render(self) -> str:
        mode = "text + vector" if self.vector_enabled else "text only"
        return (
            f"  search index ({mode}): {self.documents} documents, "
            f"{self.embedded} embedded, {self.embedding_errors} embedding errors"
        )


@dataclass
class HybridSearch:
    text_index: TextIndex
    vector_index: VectorIndex | None = None
    embedder: Embedder | None = None
    rrf_k: int = 60
    candidate_multiplier: int = 3
    embed_batch_size: int = 32
    _vector_warning_logged: bool = field(default=False, init=False, repr=False)

    @property
    def vector_enabled(self) -> bool:
        return self.vector_index is not None and self.embedder is not None

    def index(self, documents: Iterable[TextDocument]) -> IndexSummary:
        docs = list(documents)
        summary = IndexSummary(documents=len(docs), vector_enabled=self.vector_enabled)
        self.text_index.index(docs)
        if not self.vector_enabled:
            self._warn_text_only()
            return summary
        assert self.vector_index is not None and self.embedder is not None
        for start in range(0, len(docs), self.embed_batch_size):
            batch = docs[start : start + self.embed_batch_size]
            try:
                vectors = self.embedder([f"{d.title}\n{d.body}" for d in batch])
            except Exception as error:
                summary.embedding_errors += len(batch)
                log.warning("embedding batch failed: %s", type(error).__name__)
                continue
            for document, vector in zip(batch, vectors, strict=True):
                self.vector_index.upsert(document.doc_id, vector)
                summary.embedded += 1
        return summary

    def search(self, query: str, *, limit: int = 10, prefix: str | None = None) -> list[SearchHit]:
        if limit < 1 or not query.strip():
            return []
        fetch = limit * self.candidate_multiplier
        ranked: list[list[SearchHit]] = [self.text_index.search(query, limit=fetch, prefix=prefix)]
        if self.vector_enabled:
            assert self.vector_index is not None and self.embedder is not None
            try:
                vector = self.embedder([query])[0]
                ranked.append(self.vector_index.search(vector, limit=fetch, prefix=prefix))
            except Exception as error:
                log.warning("vector search unavailable (%s); text only", type(error).__name__)
        else:
            self._warn_text_only()
        return reciprocal_rank_fusion(ranked, k=self.rrf_k)[:limit]

    def remove(self, doc_ids: Iterable[str]) -> None:
        ids = list(doc_ids)
        self.text_index.remove(ids)
        if self.vector_index is not None:
            self.vector_index.remove(ids)

    def clear(self) -> None:
        self.text_index.clear()
        if self.vector_index is not None:
            self.vector_index.clear()

    def _warn_text_only(self) -> None:
        if not self._vector_warning_logged:
            log.warning("no embedding model available: search uses full-text matching only")
            self._vector_warning_logged = True


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[SearchHit]], *, k: int = 60
) -> list[SearchHit]:
    """Fuse several rankings: ``score(d) = sum(1 / (k + rank_i(d)))``."""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for position, hit in enumerate(ranking, start=1):
            fused[hit.doc_id] = fused.get(hit.doc_id, 0.0) + 1.0 / (k + position)
    ordered = sorted(fused.items(), key=lambda item: (-item[1], item[0]))
    return [SearchHit(doc_id=doc_id, score=score) for doc_id, score in ordered]
