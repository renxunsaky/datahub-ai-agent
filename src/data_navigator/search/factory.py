"""Wire the configured search implementation to the catalog storage."""

from __future__ import annotations

import logging

from data_navigator.catalog.repository import CatalogRepository
from data_navigator.catalog.sqlalchemy_repo import SqlAlchemyCatalogRepository
from data_navigator.config import Settings
from data_navigator.llm.client import LLMClient
from data_navigator.search.documents import build_documents
from data_navigator.search.hybrid import Embedder, HybridSearch, IndexSummary
from data_navigator.search.postgres_index import PostgresTextIndex, PostgresVectorIndex
from data_navigator.search.sqlite_text_index import SqliteTextIndex
from data_navigator.search.sqlite_vector_index import SqliteVectorIndex
from data_navigator.search.text_index import TextIndex
from data_navigator.search.vector_index import VectorIndex

log = logging.getLogger(__name__)


def build_search(
    settings: Settings, repository: CatalogRepository, llm: LLMClient | None
) -> HybridSearch:
    text_index: TextIndex
    vector_index: VectorIndex | None
    if settings.storage.backend == "sqlite":
        if not isinstance(repository, SqlAlchemyCatalogRepository):
            msg = "the SQLite search indexes need the SQLAlchemy repository"
            raise TypeError(msg)
        text_index = SqliteTextIndex(repository.engine)
        vector_index = SqliteVectorIndex(repository.engine)
    else:
        text_index = PostgresTextIndex(settings.storage.url)
        vector_index = PostgresVectorIndex(settings.storage.url)
    embedder: Embedder | None = None
    if llm is not None and llm.supports_embeddings:
        embedder = llm.embed
    else:
        vector_index = None
    return HybridSearch(
        text_index,
        vector_index,
        embedder,
        rrf_k=settings.search.rrf_k,
        candidate_multiplier=settings.search.candidate_multiplier,
    )


def reindex(repository: CatalogRepository, search: HybridSearch) -> IndexSummary:
    """Rebuild the whole index from the catalog (idempotent)."""
    search.clear()
    return search.index(build_documents(repository))
