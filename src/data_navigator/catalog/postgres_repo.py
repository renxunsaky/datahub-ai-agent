"""PostgreSQL catalog repository.

Placeholder for a later phase.  The SQLAlchemy base implementation and the
Alembic migrations are dialect-neutral, so the real implementation should only
need an engine (and, later, ``JSONB``/``pgvector`` column tweaks).

TODO(postgres): validate migrations and the repository against a real PostgreSQL
instance, add connection pooling settings, and switch ``TextIndex``/``VectorIndex``
to PostgreSQL full-text search and pgvector.
"""

from __future__ import annotations

from data_navigator.catalog.sqlalchemy_repo import SqlAlchemyCatalogRepository


class PostgresCatalogRepository(SqlAlchemyCatalogRepository):
    def __init__(self, url: str) -> None:
        msg = (
            "The PostgreSQL backend is not implemented yet (storage.backend=postgres). "
            "Use storage.backend=sqlite for the MVP."
        )
        raise NotImplementedError(msg)
