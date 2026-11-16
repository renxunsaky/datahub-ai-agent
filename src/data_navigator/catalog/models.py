"""SQLAlchemy ORM models for the catalog database.

The models use only portable constructs (no SQLite-specific types or options) so
the same metadata and Alembic migrations work on PostgreSQL.  JSON columns hold
small lists/dicts; on PostgreSQL they may later be switched to ``JSONB``.

Sample data and raw SQL constants are never stored here: ``query_templates``
holds normalised SQL only, and ``parse_failures`` holds hashes only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from data_navigator.catalog.types import UTCDateTime


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime: UTCDateTime,
        dict[str, Any]: JSON,
        list[str]: JSON,
        list[dict[str, Any]]: JSON,
    }


class CatalogTable(Base):
    __tablename__ = "catalog_tables"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    full_name: Mapped[str] = mapped_column(String(512), unique=True)
    catalog: Mapped[str] = mapped_column(String(128))
    schema_name: Mapped[str] = mapped_column(String(128))
    table_name: Mapped[str] = mapped_column(String(256))
    table_type: Mapped[str | None] = mapped_column(String(64))
    comment: Mapped[str | None] = mapped_column(Text)
    owner_team: Mapped[str | None] = mapped_column(String(128))
    # ``inferred`` (from naming rules) or ``confirmed`` (set by a person; never overwritten).
    owner_source: Mapped[str | None] = mapped_column(String(32))
    # SHA-256 over the ordered (column name, type) pairs; used to detect structure changes.
    structure_hash: Mapped[str] = mapped_column(String(64))
    last_queried_at: Mapped[datetime | None]
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    user_count: Mapped[int] = mapped_column(Integer, default=0)
    team_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    deleted_at: Mapped[datetime | None]

    columns: Mapped[list[CatalogColumn]] = relationship(
        back_populates="table",
        cascade="all, delete-orphan",
        order_by="CatalogColumn.ordinal_position",
    )
    card: Mapped[TableCard | None] = relationship(
        back_populates="table", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (Index("ix_catalog_tables_schema", "catalog", "schema_name"),)


class CatalogColumn(Base):
    __tablename__ = "catalog_columns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    table_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_tables.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(256))
    ordinal_position: Mapped[int] = mapped_column(Integer)
    data_type: Mapped[str] = mapped_column(String(256))
    comment: Mapped[str | None] = mapped_column(Text)
    ai_description: Mapped[str | None] = mapped_column(Text)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    join_key_count: Mapped[int] = mapped_column(Integer, default=0)
    filter_count: Mapped[int] = mapped_column(Integer, default=0)
    is_join_key: Mapped[bool] = mapped_column(Boolean, default=False)
    is_filter: Mapped[bool] = mapped_column(Boolean, default=False)
    # ``None`` = not assessed; ``pii_source`` is ``inferred`` or ``confirmed``.
    pii_flag: Mapped[bool | None] = mapped_column(Boolean)
    pii_source: Mapped[str | None] = mapped_column(String(32))

    table: Mapped[CatalogTable] = relationship(back_populates="columns")

    __table_args__ = (UniqueConstraint("table_id", "name", name="uq_catalog_columns_table_name"),)


class TableCard(Base):
    __tablename__ = "table_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    table_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_tables.id", ondelete="CASCADE"), unique=True
    )
    purpose: Mapped[str | None] = mapped_column(Text)
    key_columns: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    common_usage: Mapped[str | None] = mapped_column(Text)
    example_questions: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[float | None] = mapped_column(Float)
    # ai_inferred | confirmed | rejected
    review_status: Mapped[str] = mapped_column(String(32), default="ai_inferred")
    reviewed_by: Mapped[str | None] = mapped_column(String(128))
    reviewed_at: Mapped[datetime | None]
    # Field names edited by a reviewer; regeneration must leave them untouched.
    confirmed_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_hash: Mapped[str | None] = mapped_column(String(64))
    generated_at: Mapped[datetime | None]
    model: Mapped[str | None] = mapped_column(String(128))

    table: Mapped[CatalogTable] = relationship(back_populates="card")


class JoinEdge(Base):
    __tablename__ = "join_edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    left_table: Mapped[str] = mapped_column(String(512))
    left_column: Mapped[str] = mapped_column(String(256))
    right_table: Mapped[str] = mapped_column(String(512))
    right_column: Mapped[str] = mapped_column(String(256))
    join_type: Mapped[str] = mapped_column(String(32))
    occurrence_count: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    # Per-source counts, e.g. {"repo_file": 2, "repeated_query": 1, "single_query": 3}.
    sources: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (
        UniqueConstraint(
            "left_table",
            "left_column",
            "right_table",
            "right_column",
            "join_type",
            name="uq_join_edges_key",
        ),
        Index("ix_join_edges_left", "left_table"),
        Index("ix_join_edges_right", "right_table"),
    )


class LineageEdge(Base):
    __tablename__ = "lineage_edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    upstream_table: Mapped[str] = mapped_column(String(512))
    downstream_table: Mapped[str] = mapped_column(String(512))
    source_path: Mapped[str] = mapped_column(String(1024))
    statement_type: Mapped[str] = mapped_column(String(32))

    __table_args__ = (
        UniqueConstraint(
            "upstream_table", "downstream_table", "source_path", name="uq_lineage_edges_key"
        ),
        Index("ix_lineage_edges_downstream", "downstream_table"),
    )


class QueryTemplate(Base):
    __tablename__ = "query_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    normalized_sql: Mapped[str] = mapped_column(Text)
    dialect: Mapped[str] = mapped_column(String(16))
    statement_type: Mapped[str] = mapped_column(String(32))
    tables_read: Mapped[list[str]] = mapped_column(JSON, default=list)
    tables_written: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Full ParsedQuery payload (columns per table, join edges, filters, aggregates).
    parsed: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    execution_count: Mapped[int] = mapped_column(Integer, default=0)
    users: Mapped[list[str]] = mapped_column(JSON, default=list)
    teams: Mapped[list[str]] = mapped_column(JSON, default=list)
    # "history" and/or "file".
    source_kinds: Mapped[list[str]] = mapped_column(JSON, default=list)
    file_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    first_seen_at: Mapped[datetime]
    last_executed_at: Mapped[datetime | None]


class SqlFile(Base):
    __tablename__ = "sql_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    path: Mapped[str] = mapped_column(String(1024), unique=True)
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    statement_count: Mapped[int] = mapped_column(Integer, default=0)
    parse_failure_count: Mapped[int] = mapped_column(Integer, default=0)
    fingerprints: Mapped[list[str]] = mapped_column(JSON, default=list)
    processed_at: Mapped[datetime]


class ParseFailure(Base):
    __tablename__ = "parse_failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # SHA-256 of the original statement text; the text itself is never stored.
    sql_hash: Mapped[str] = mapped_column(String(64))
    dialects_tried: Mapped[str] = mapped_column(String(64))
    error_type: Mapped[str] = mapped_column(String(128))
    source_kind: Mapped[str] = mapped_column(String(16))
    source_ref: Mapped[str | None] = mapped_column(String(1024))
    count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]

    __table_args__ = (
        UniqueConstraint("sql_hash", "source_kind", "source_ref", name="uq_parse_failures_key"),
    )


class BuildRun(Base):
    __tablename__ = "build_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    history_processed_until: Mapped[datetime | None]
    repo_commit_sha: Mapped[str | None] = mapped_column(String(64))
    tables_added: Mapped[int] = mapped_column(Integer, default=0)
    tables_changed: Mapped[int] = mapped_column(Integer, default=0)
    tables_removed: Mapped[int] = mapped_column(Integer, default=0)
    queries_processed: Mapped[int] = mapped_column(Integer, default=0)
    files_processed: Mapped[int] = mapped_column(Integer, default=0)
    parse_failures: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime]
    user: Mapped[str] = mapped_column(String(128))
    question: Mapped[str] = mapped_column(Text)
    # Normalised SQL only (constants replaced by placeholders).
    generated_sql: Mapped[str | None] = mapped_column(Text)
    validation_status: Mapped[str | None] = mapped_column(String(32))
    sample_confirmed: Mapped[bool | None] = mapped_column(Boolean)
    sample_executed: Mapped[bool] = mapped_column(Boolean, default=False)
    sample_row_count: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (Index("ix_audit_logs_user_time", "user", "occurred_at"),)
