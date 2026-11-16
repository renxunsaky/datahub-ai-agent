"""Create the configured catalog repository."""

from __future__ import annotations

from pathlib import Path

from data_navigator.catalog.postgres_repo import PostgresCatalogRepository
from data_navigator.catalog.repository import CatalogRepository
from data_navigator.catalog.sqlite_repo import SqliteCatalogRepository
from data_navigator.config import Settings, StorageSettings


def create_repository(storage: StorageSettings) -> CatalogRepository:
    if storage.backend == "sqlite":
        return SqliteCatalogRepository(storage.url)
    return PostgresCatalogRepository(storage.url)


def open_repository(settings: Settings, *, dry_run: bool) -> CatalogRepository:
    """Open the catalog; a dry run never creates or migrates a database on disk."""
    if dry_run and settings.storage.backend == "sqlite":
        path = sqlite_path(settings.storage.url)
        if path is None or not path.exists():
            memory = settings.storage.model_copy(update={"url": "sqlite://"})
            repo = create_repository(memory)
            repo.initialize()
            return repo
        return create_repository(settings.storage)
    repo = create_repository(settings.storage)
    repo.initialize()
    return repo


def sqlite_path(url: str) -> Path | None:
    if "///" not in url:
        return None
    raw = url.split("///", 1)[1]
    if not raw or raw == ":memory:":
        return None
    return Path(raw).expanduser()
