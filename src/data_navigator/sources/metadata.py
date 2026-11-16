"""Table and column metadata."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from sqlglot import exp

from data_navigator.catalog.records import ColumnRecord, TableRecord
from data_navigator.catalog.types import utcnow
from data_navigator.config import MetadataSettings
from data_navigator.starburst.builder_client import BuilderClient, BuilderQueryError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetadataSnapshot:
    tables: list[TableRecord]
    fetched_at: datetime


class MetadataSource(Protocol):
    def fetch(self) -> MetadataSnapshot: ...


def full_name(catalog: str, schema: str, table: str) -> str:
    return f"{catalog}.{schema}.{table}".lower()


# ---------------------------------------------------------------------------
# Fixture implementation
# ---------------------------------------------------------------------------


class FixtureMetadataSource:
    """Reads ``tables.json``, ``columns.json`` and ``table_comments.json`` from a directory.

    The JSON rows mirror ``information_schema.tables`` / ``.columns`` and
    ``system.metadata.table_comments`` so the same assembly code serves both.
    """

    def __init__(
        self,
        directory: Path,
        *,
        catalogs: Sequence[str] | None = None,
        excluded_schemas: Sequence[str] = (),
    ) -> None:
        self._directory = directory
        self._catalogs = {c.lower() for c in catalogs} if catalogs else None
        self._excluded = {s.lower() for s in excluded_schemas}

    def fetch(self) -> MetadataSnapshot:
        tables = _load_json(self._directory / "tables.json")
        columns = _load_json(self._directory / "columns.json")
        comments_path = self._directory / "table_comments.json"
        comments = _load_json(comments_path) if comments_path.exists() else []
        records = assemble_tables(
            tables, columns, comments, catalogs=self._catalogs, excluded_schemas=self._excluded
        )
        return MetadataSnapshot(tables=records, fetched_at=utcnow())


def _load_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        msg = f"{path}: expected a JSON list"
        raise ValueError(msg)
    return data


# ---------------------------------------------------------------------------
# Starburst implementation
# ---------------------------------------------------------------------------


class StarburstMetadataSource:
    """Reads metadata through the whitelisted builder client."""

    def __init__(
        self, client: BuilderClient, settings: MetadataSettings, *, dialect: str = "trino"
    ) -> None:
        self._client = client
        self._settings = settings
        self._dialect = dialect

    def fetch(self) -> MetadataSnapshot:
        tables: list[dict[str, Any]] = []
        columns: list[dict[str, Any]] = []
        comments: list[dict[str, Any]] = []
        need_show_columns: list[str] = []
        for catalog in self._settings.catalogs:
            tables.extend(self._query_tables(catalog))
            catalog_columns, has_comments = self._query_columns(catalog)
            columns.extend(catalog_columns)
            if not has_comments:
                need_show_columns.append(catalog)
            comments.extend(self._query_table_comments(catalog))
        records = assemble_tables(
            tables,
            columns,
            comments,
            catalogs=None,
            excluded_schemas=set(self._settings.excluded_schemas),
        )
        if need_show_columns:
            records = self._fill_column_comments(records, set(need_show_columns))
        return MetadataSnapshot(tables=records, fetched_at=utcnow())

    # -- queries ---------------------------------------------------------------------

    def _query_tables(self, catalog: str) -> list[dict[str, Any]]:
        query = (
            exp.select("table_catalog", "table_schema", "table_name", "table_type")
            .from_(_info_schema_table(catalog, "tables"))
            .where(self._schema_filter())
        )
        result = self._client.query(query.sql(dialect=self._dialect, identify=True))
        return [dict(zip(result.columns, row, strict=True)) for row in result.rows]

    def _query_columns(self, catalog: str) -> tuple[list[dict[str, Any]], bool]:
        strategy = self._settings.column_comments
        base = [
            "table_catalog",
            "table_schema",
            "table_name",
            "column_name",
            "ordinal_position",
            "data_type",
        ]
        attempts = [[*base, "comment"], base] if strategy != "show_columns" else [base]
        last_error: BuilderQueryError | None = None
        for selected in attempts:
            query = (
                exp.select(*selected)
                .from_(_info_schema_table(catalog, "columns"))
                .where(self._schema_filter())
            )
            try:
                result = self._client.query(query.sql(dialect=self._dialect, identify=True))
            except BuilderQueryError as error:
                if strategy == "information_schema" or error.mapped.status != "semantic_error":
                    raise
                last_error = error
                log.info("information_schema.columns has no comment column; using SHOW COLUMNS")
                continue
            rows = [dict(zip(result.columns, row, strict=True)) for row in result.rows]
            return rows, "comment" in selected
        assert last_error is not None
        raise last_error

    def _query_table_comments(self, catalog: str) -> list[dict[str, Any]]:
        query = (
            exp.select("catalog_name", "schema_name", "table_name", "comment")
            .from_(
                exp.Table(
                    this=exp.to_identifier("table_comments"),
                    db=exp.to_identifier("metadata"),
                    catalog=exp.to_identifier("system"),
                )
            )
            .where(exp.column("catalog_name").eq(exp.Literal.string(catalog)))
        )
        try:
            result = self._client.query(query.sql(dialect=self._dialect, identify=True))
        except BuilderQueryError as error:
            log.warning(
                "table comments unavailable for catalog %s: %s", catalog, error.mapped.status
            )
            return []
        return [dict(zip(result.columns, row, strict=True)) for row in result.rows]

    def _fill_column_comments(
        self, records: list[TableRecord], catalogs: set[str]
    ) -> list[TableRecord]:
        filled: list[TableRecord] = []
        for record in records:
            if record.catalog not in catalogs:
                filled.append(record)
                continue
            table_sql = exp.Table(
                this=exp.to_identifier(record.table_name),
                db=exp.to_identifier(record.schema_name),
                catalog=exp.to_identifier(record.catalog),
            ).sql(dialect=self._dialect, identify=True)
            try:
                result = self._client.query(f"SHOW COLUMNS FROM {table_sql}")
            except BuilderQueryError as error:
                log.warning("SHOW COLUMNS failed for %s: %s", record.full_name, error.mapped.status)
                filled.append(record)
                continue
            comments = {
                str(row[0]).lower(): (str(row[3]) if len(row) > 3 and row[3] else None)
                for row in result.rows
            }
            filled.append(
                TableRecord(
                    full_name=record.full_name,
                    catalog=record.catalog,
                    schema_name=record.schema_name,
                    table_name=record.table_name,
                    table_type=record.table_type,
                    comment=record.comment,
                    columns=tuple(
                        ColumnRecord(
                            name=c.name,
                            ordinal_position=c.ordinal_position,
                            data_type=c.data_type,
                            comment=comments.get(c.name.lower(), c.comment),
                        )
                        for c in record.columns
                    ),
                )
            )
        return filled

    def _schema_filter(self) -> exp.Expr:
        excluded = [exp.Literal.string(s) for s in self._settings.excluded_schemas]
        if not excluded:
            return exp.true()
        return exp.Not(this=exp.In(this=exp.column("table_schema"), expressions=excluded))


def _info_schema_table(catalog: str, name: str) -> exp.Table:
    return exp.Table(
        this=exp.to_identifier(name),
        db=exp.to_identifier("information_schema"),
        catalog=exp.to_identifier(catalog),
    )


# ---------------------------------------------------------------------------
# Shared assembly
# ---------------------------------------------------------------------------


def assemble_tables(
    tables: Iterable[dict[str, Any]],
    columns: Iterable[dict[str, Any]],
    comments: Iterable[dict[str, Any]],
    *,
    catalogs: set[str] | None,
    excluded_schemas: set[str],
) -> list[TableRecord]:
    """Combine information_schema-style rows into :class:`TableRecord` objects."""
    comment_by_table = {
        full_name(str(c["catalog_name"]), str(c["schema_name"]), str(c["table_name"])): c.get(
            "comment"
        )
        for c in comments
    }
    columns_by_table: dict[str, list[ColumnRecord]] = {}
    for row in columns:
        key = full_name(str(row["table_catalog"]), str(row["table_schema"]), str(row["table_name"]))
        columns_by_table.setdefault(key, []).append(
            ColumnRecord(
                name=str(row["column_name"]).lower(),
                ordinal_position=int(row["ordinal_position"]),
                data_type=str(row["data_type"]).lower(),
                comment=_clean(row.get("comment")),
            )
        )
    records: list[TableRecord] = []
    for row in tables:
        catalog = str(row["table_catalog"]).lower()
        schema = str(row["table_schema"]).lower()
        name = str(row["table_name"]).lower()
        if catalogs is not None and catalog not in catalogs:
            continue
        if schema in excluded_schemas:
            continue
        key = full_name(catalog, schema, name)
        ordered = sorted(columns_by_table.get(key, []), key=lambda c: (c.ordinal_position, c.name))
        records.append(
            TableRecord(
                full_name=key,
                catalog=catalog,
                schema_name=schema,
                table_name=name,
                table_type=_clean(row.get("table_type")),
                comment=_clean(comment_by_table.get(key)),
                columns=tuple(ordered),
            )
        )
    records.sort(key=lambda r: r.full_name)
    return records


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
