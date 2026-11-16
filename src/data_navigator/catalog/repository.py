"""The storage interface every catalog backend implements.

The MVP backend is SQLite (``sqlite_repo``); PostgreSQL (``postgres_repo``) is a
stub.  Business code depends on this interface only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from data_navigator.catalog.records import (
    BuildRunRecord,
    JoinEdgeRecord,
    LineageEdgeRecord,
    ParseFailureRecord,
    SqlFileRecord,
    TableDiff,
    TableRecord,
    TableState,
    TableUsage,
    TemplateRecord,
)

SchemaMap = dict[str, dict[str, dict[str, dict[str, str]]]]


class CatalogRepository(ABC):
    """Relational storage for the catalog."""

    # -- lifecycle -----------------------------------------------------------------

    @abstractmethod
    def initialize(self) -> None:
        """Create or migrate the schema."""

    @abstractmethod
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group several calls into one atomic unit (nested calls join the outer one)."""

    # -- tables ----------------------------------------------------------------------

    @abstractmethod
    def list_tables(self, *, include_deleted: bool = False) -> list[TableState]: ...

    @abstractmethod
    def get_table(self, full_name: str) -> TableState | None: ...

    @abstractmethod
    def upsert_tables(self, tables: Iterable[TableRecord], *, seen_at: datetime) -> TableDiff:
        """Insert new tables, update changed ones; never touches confirmed fields."""

    @abstractmethod
    def mark_tables_deleted(self, full_names: Iterable[str], *, at: datetime) -> list[str]:
        """Soft-delete tables that disappeared; returns the names newly marked."""

    @abstractmethod
    def set_owner(self, full_name: str, team: str | None, *, source: str) -> bool:
        """Set the owning team unless a person confirmed it; returns whether it changed."""

    @abstractmethod
    def schema_map(self) -> SchemaMap:
        """``{catalog: {schema: {table: {column: type}}}}`` for live tables."""

    # -- query templates / files / failures --------------------------------------------

    @abstractmethod
    def list_templates(self) -> list[TemplateRecord]: ...

    @abstractmethod
    def upsert_templates(self, templates: Iterable[TemplateRecord]) -> None:
        """Merge by fingerprint: counts add up, users/teams/paths are unioned."""

    @abstractmethod
    def clear_templates(self) -> None: ...

    @abstractmethod
    def detach_file(self, path: str) -> None:
        """Forget a file's contribution to templates (before re-processing it)."""

    @abstractmethod
    def list_sql_files(self) -> list[SqlFileRecord]: ...

    @abstractmethod
    def upsert_sql_files(self, files: Iterable[SqlFileRecord]) -> None: ...

    @abstractmethod
    def delete_sql_files(self, paths: Iterable[str]) -> None: ...

    @abstractmethod
    def record_parse_failures(
        self, failures: Iterable[ParseFailureRecord], *, seen_at: datetime
    ) -> None: ...

    @abstractmethod
    def count_parse_failures(self) -> int: ...

    # -- derived data ----------------------------------------------------------------

    @abstractmethod
    def replace_join_edges(self, edges: Iterable[JoinEdgeRecord]) -> None: ...

    @abstractmethod
    def list_join_edges(self) -> list[JoinEdgeRecord]: ...

    @abstractmethod
    def replace_lineage_edges(self, edges: Iterable[LineageEdgeRecord]) -> None: ...

    @abstractmethod
    def list_lineage_edges(self) -> list[LineageEdgeRecord]: ...

    @abstractmethod
    def update_usage(self, usages: Iterable[TableUsage], *, reset_missing: bool = True) -> None:
        """Write usage statistics; tables not listed are reset to zero when asked."""

    # -- build runs ------------------------------------------------------------------

    @abstractmethod
    def start_build_run(self, run_type: str, *, started_at: datetime) -> int: ...

    @abstractmethod
    def finish_build_run(
        self, run_id: int, *, status: str, finished_at: datetime, **fields: Any
    ) -> None: ...

    @abstractmethod
    def last_successful_run(self) -> BuildRunRecord | None: ...

    @abstractmethod
    def list_build_runs(self) -> list[BuildRunRecord]: ...
