"""Create the configured catalog repository."""

from __future__ import annotations

from data_navigator.catalog.postgres_repo import PostgresCatalogRepository
from data_navigator.catalog.repository import CatalogRepository
from data_navigator.catalog.sqlite_repo import SqliteCatalogRepository
from data_navigator.config import StorageSettings


def create_repository(storage: StorageSettings) -> CatalogRepository:
    if storage.backend == "sqlite":
        return SqliteCatalogRepository(storage.url)
    return PostgresCatalogRepository(storage.url)
