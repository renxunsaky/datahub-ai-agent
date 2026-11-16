"""initial catalog schema

Revision ID: 0001
Revises:
Create Date: 2026-11-16 14:17:46.390742
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from data_navigator.catalog.types import UTCDateTime

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("occurred_at", UTCDateTime(), nullable=False),
        sa.Column("user", sa.String(length=128), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("generated_sql", sa.Text(), nullable=True),
        sa.Column("validation_status", sa.String(length=32), nullable=True),
        sa.Column("sample_confirmed", sa.Boolean(), nullable=True),
        sa.Column("sample_executed", sa.Boolean(), nullable=False),
        sa.Column("sample_row_count", sa.Integer(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.create_index("ix_audit_logs_user_time", ["user", "occurred_at"], unique=False)

    op.create_table(
        "build_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", UTCDateTime(), nullable=False),
        sa.Column("finished_at", UTCDateTime(), nullable=True),
        sa.Column("history_processed_until", UTCDateTime(), nullable=True),
        sa.Column("repo_commit_sha", sa.String(length=64), nullable=True),
        sa.Column("tables_added", sa.Integer(), nullable=False),
        sa.Column("tables_changed", sa.Integer(), nullable=False),
        sa.Column("tables_removed", sa.Integer(), nullable=False),
        sa.Column("queries_processed", sa.Integer(), nullable=False),
        sa.Column("files_processed", sa.Integer(), nullable=False),
        sa.Column("parse_failures", sa.Integer(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "catalog_tables",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("full_name", sa.String(length=512), nullable=False),
        sa.Column("catalog", sa.String(length=128), nullable=False),
        sa.Column("schema_name", sa.String(length=128), nullable=False),
        sa.Column("table_name", sa.String(length=256), nullable=False),
        sa.Column("table_type", sa.String(length=64), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("owner_team", sa.String(length=128), nullable=True),
        sa.Column("owner_source", sa.String(length=32), nullable=True),
        sa.Column("structure_hash", sa.String(length=64), nullable=False),
        sa.Column("last_queried_at", UTCDateTime(), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False),
        sa.Column("user_count", sa.Integer(), nullable=False),
        sa.Column("team_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", UTCDateTime(), nullable=False),
        sa.Column("last_seen_at", UTCDateTime(), nullable=False),
        sa.Column("deleted_at", UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("full_name"),
    )
    with op.batch_alter_table("catalog_tables", schema=None) as batch_op:
        batch_op.create_index("ix_catalog_tables_schema", ["catalog", "schema_name"], unique=False)

    op.create_table(
        "join_edges",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("left_table", sa.String(length=512), nullable=False),
        sa.Column("left_column", sa.String(length=256), nullable=False),
        sa.Column("right_table", sa.String(length=512), nullable=False),
        sa.Column("right_column", sa.String(length=256), nullable=False),
        sa.Column("join_type", sa.String(length=32), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "left_table",
            "left_column",
            "right_table",
            "right_column",
            "join_type",
            name="uq_join_edges_key",
        ),
    )
    with op.batch_alter_table("join_edges", schema=None) as batch_op:
        batch_op.create_index("ix_join_edges_left", ["left_table"], unique=False)
        batch_op.create_index("ix_join_edges_right", ["right_table"], unique=False)

    op.create_table(
        "lineage_edges",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("upstream_table", sa.String(length=512), nullable=False),
        sa.Column("downstream_table", sa.String(length=512), nullable=False),
        sa.Column("source_path", sa.String(length=1024), nullable=False),
        sa.Column("statement_type", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "upstream_table", "downstream_table", "source_path", name="uq_lineage_edges_key"
        ),
    )
    with op.batch_alter_table("lineage_edges", schema=None) as batch_op:
        batch_op.create_index("ix_lineage_edges_downstream", ["downstream_table"], unique=False)

    op.create_table(
        "parse_failures",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sql_hash", sa.String(length=64), nullable=False),
        sa.Column("dialects_tried", sa.String(length=64), nullable=False),
        sa.Column("error_type", sa.String(length=128), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("source_ref", sa.String(length=1024), nullable=True),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", UTCDateTime(), nullable=False),
        sa.Column("last_seen_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sql_hash", "source_kind", "source_ref", name="uq_parse_failures_key"),
    )
    op.create_table(
        "query_templates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("normalized_sql", sa.Text(), nullable=False),
        sa.Column("dialect", sa.String(length=16), nullable=False),
        sa.Column("statement_type", sa.String(length=32), nullable=False),
        sa.Column("tables_read", sa.JSON(), nullable=False),
        sa.Column("tables_written", sa.JSON(), nullable=False),
        sa.Column("parsed", sa.JSON(), nullable=False),
        sa.Column("execution_count", sa.Integer(), nullable=False),
        sa.Column("users", sa.JSON(), nullable=False),
        sa.Column("teams", sa.JSON(), nullable=False),
        sa.Column("source_kinds", sa.JSON(), nullable=False),
        sa.Column("file_paths", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", UTCDateTime(), nullable=False),
        sa.Column("last_executed_at", UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint"),
    )
    op.create_table(
        "sql_files",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("statement_count", sa.Integer(), nullable=False),
        sa.Column("parse_failure_count", sa.Integer(), nullable=False),
        sa.Column("fingerprints", sa.JSON(), nullable=False),
        sa.Column("processed_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("path"),
    )
    op.create_table(
        "catalog_columns",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("table_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("ordinal_position", sa.Integer(), nullable=False),
        sa.Column("data_type", sa.String(length=256), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("ai_description", sa.Text(), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False),
        sa.Column("join_key_count", sa.Integer(), nullable=False),
        sa.Column("filter_count", sa.Integer(), nullable=False),
        sa.Column("is_join_key", sa.Boolean(), nullable=False),
        sa.Column("is_filter", sa.Boolean(), nullable=False),
        sa.Column("pii_flag", sa.Boolean(), nullable=True),
        sa.Column("pii_source", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(["table_id"], ["catalog_tables.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("table_id", "name", name="uq_catalog_columns_table_name"),
    )
    with op.batch_alter_table("catalog_columns", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_catalog_columns_table_id"), ["table_id"], unique=False)

    op.create_table(
        "table_cards",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("table_id", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("key_columns", sa.JSON(), nullable=False),
        sa.Column("common_usage", sa.Text(), nullable=True),
        sa.Column("example_questions", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("review_status", sa.String(length=32), nullable=False),
        sa.Column("reviewed_by", sa.String(length=128), nullable=True),
        sa.Column("reviewed_at", UTCDateTime(), nullable=True),
        sa.Column("confirmed_fields", sa.JSON(), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=True),
        sa.Column("generated_at", UTCDateTime(), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(["table_id"], ["catalog_tables.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("table_id"),
    )


def downgrade() -> None:
    op.drop_table("table_cards")
    with op.batch_alter_table("catalog_columns", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_catalog_columns_table_id"))

    op.drop_table("catalog_columns")
    op.drop_table("sql_files")
    op.drop_table("query_templates")
    op.drop_table("parse_failures")
    with op.batch_alter_table("lineage_edges", schema=None) as batch_op:
        batch_op.drop_index("ix_lineage_edges_downstream")

    op.drop_table("lineage_edges")
    with op.batch_alter_table("join_edges", schema=None) as batch_op:
        batch_op.drop_index("ix_join_edges_right")
        batch_op.drop_index("ix_join_edges_left")

    op.drop_table("join_edges")
    with op.batch_alter_table("catalog_tables", schema=None) as batch_op:
        batch_op.drop_index("ix_catalog_tables_schema")

    op.drop_table("catalog_tables")
    op.drop_table("build_runs")
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.drop_index("ix_audit_logs_user_time")

    op.drop_table("audit_logs")
