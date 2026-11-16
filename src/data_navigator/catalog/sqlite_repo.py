"""SQLite catalog repository (the MVP backend)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from data_navigator.catalog.sqlalchemy_repo import SqlAlchemyCatalogRepository


def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def make_sqlite_engine(url: str) -> Engine:
    """Create a SQLite engine with foreign keys enabled and the parent directory present."""
    if not url.startswith("sqlite"):
        msg = f"not a SQLite URL: {url}"
        raise ValueError(msg)
    path = url.split("///", 1)[1] if "///" in url else ""
    if path and path != ":memory:":
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True)
    event.listen(engine, "connect", _enable_foreign_keys)
    return engine


class SqliteCatalogRepository(SqlAlchemyCatalogRepository):
    def __init__(self, url: str) -> None:
        super().__init__(make_sqlite_engine(url))
