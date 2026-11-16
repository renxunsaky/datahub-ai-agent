"""SQLAlchemy implementation of :class:`CatalogRepository`.

Dialect-neutral; the SQLite and PostgreSQL repositories subclass it and only
differ in how the engine is created.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from data_navigator.catalog import models as m
from data_navigator.catalog.records import (
    CARD_EDITABLE_FIELDS,
    CARD_STATUS_AI,
    CARD_STATUSES,
    AuditRecord,
    BuildRunRecord,
    CardDraft,
    CardRecord,
    ColumnState,
    JoinEdgeRecord,
    KeyColumn,
    LineageEdgeRecord,
    ParseFailureRecord,
    SqlFileRecord,
    TableDiff,
    TableRecord,
    TableState,
    TableUsage,
    TemplateRecord,
)
from data_navigator.catalog.repository import CatalogRepository, SchemaMap
from data_navigator.catalog.types import utcnow

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

OWNER_CONFIRMED = "confirmed"


def alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


class SqlAlchemyCatalogRepository(CatalogRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory = sessionmaker(engine, expire_on_commit=False)
        self._session: Session | None = None

    @property
    def engine(self) -> Engine:
        return self._engine

    # -- lifecycle -----------------------------------------------------------------

    def initialize(self) -> None:
        config = alembic_config()
        with self._engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._session is not None:
            yield
            return
        session = self._session_factory()
        self._session = session
        try:
            with session.begin():
                yield
        finally:
            self._session = None
            session.close()

    @contextmanager
    def _scope(self) -> Iterator[Session]:
        with self.transaction():
            assert self._session is not None
            yield self._session

    # -- tables ----------------------------------------------------------------------

    def list_tables(self, *, include_deleted: bool = False) -> list[TableState]:
        with self._scope() as session:
            query = select(m.CatalogTable).options(selectinload(m.CatalogTable.columns))
            if not include_deleted:
                query = query.where(m.CatalogTable.deleted_at.is_(None))
            query = query.order_by(m.CatalogTable.full_name)
            return [_table_state(row) for row in session.scalars(query)]

    def get_table(self, full_name: str) -> TableState | None:
        with self._scope() as session:
            row = self._get_table_row(session, full_name.lower())
            return _table_state(row) if row is not None else None

    def upsert_tables(self, tables: Iterable[TableRecord], *, seen_at: datetime) -> TableDiff:
        diff = TableDiff()
        with self._scope() as session:
            for record in tables:
                row = self._get_table_row(session, record.full_name.lower())
                if row is None:
                    row = m.CatalogTable(
                        full_name=record.full_name.lower(),
                        catalog=record.catalog,
                        schema_name=record.schema_name,
                        table_name=record.table_name,
                        table_type=record.table_type,
                        comment=record.comment,
                        structure_hash=record.structure_hash,
                        first_seen_at=seen_at,
                        last_seen_at=seen_at,
                    )
                    session.add(row)
                    diff.added.append(row.full_name)
                else:
                    was_deleted = row.deleted_at is not None
                    structure_changed = row.structure_hash != record.structure_hash
                    row.deleted_at = None
                    row.last_seen_at = seen_at
                    row.table_type = record.table_type
                    row.comment = record.comment
                    row.structure_hash = record.structure_hash
                    if was_deleted:
                        diff.added.append(row.full_name)
                    elif structure_changed:
                        diff.changed.append(row.full_name)
                    else:
                        diff.unchanged.append(row.full_name)
                _sync_columns(row, record)
            session.flush()
        return diff

    def mark_tables_deleted(self, full_names: Iterable[str], *, at: datetime) -> list[str]:
        marked: list[str] = []
        with self._scope() as session:
            for name in full_names:
                row = self._get_table_row(session, name.lower())
                if row is not None and row.deleted_at is None:
                    row.deleted_at = at
                    marked.append(row.full_name)
        return marked

    def set_owner(self, full_name: str, team: str | None, *, source: str) -> bool:
        with self._scope() as session:
            row = self._get_table_row(session, full_name.lower())
            if row is None:
                return False
            if row.owner_source == OWNER_CONFIRMED and source != OWNER_CONFIRMED:
                return False
            if row.owner_team == team and row.owner_source == source:
                return False
            row.owner_team = team
            row.owner_source = source if team is not None else None
            return True

    def schema_map(self) -> SchemaMap:
        schema: SchemaMap = {}
        for table in self.list_tables():
            columns = {column.name: column.data_type for column in table.columns}
            schema.setdefault(table.catalog, {}).setdefault(table.schema_name, {})[
                table.table_name
            ] = columns
        return schema

    @staticmethod
    def _get_table_row(session: Session, full_name: str) -> m.CatalogTable | None:
        query = (
            select(m.CatalogTable)
            .options(selectinload(m.CatalogTable.columns))
            .where(m.CatalogTable.full_name == full_name)
        )
        return session.scalars(query).one_or_none()

    # -- query templates / files / failures --------------------------------------------

    def list_templates(self) -> list[TemplateRecord]:
        with self._scope() as session:
            rows = session.scalars(select(m.QueryTemplate).order_by(m.QueryTemplate.fingerprint))
            return [_template_record(row) for row in rows]

    def upsert_templates(self, templates: Iterable[TemplateRecord]) -> None:
        with self._scope() as session:
            for record in templates:
                row = session.scalars(
                    select(m.QueryTemplate).where(m.QueryTemplate.fingerprint == record.fingerprint)
                ).one_or_none()
                first_seen = record.first_seen_at or utcnow()
                if row is None:
                    row = m.QueryTemplate(
                        fingerprint=record.fingerprint,
                        normalized_sql=record.normalized_sql,
                        dialect=record.dialect,
                        statement_type=record.statement_type,
                        first_seen_at=first_seen,
                        execution_count=0,
                        users=[],
                        teams=[],
                        file_paths=[],
                    )
                    session.add(row)
                row.normalized_sql = record.normalized_sql
                row.dialect = record.dialect
                row.statement_type = record.statement_type
                row.tables_read = sorted(set(record.tables_read))
                row.tables_written = sorted(set(record.tables_written))
                row.parsed = dict(record.parsed)
                row.execution_count = row.execution_count + record.execution_count
                row.users = sorted(set(row.users) | set(record.users))
                row.teams = sorted(set(row.teams) | set(record.teams))
                row.file_paths = sorted(set(row.file_paths) | set(record.file_paths))
                row.first_seen_at = min(row.first_seen_at, first_seen)
                row.last_executed_at = _max_optional(row.last_executed_at, record.last_executed_at)
                row.source_kinds = _source_kinds(row.execution_count, row.file_paths)
            session.flush()

    def clear_templates(self) -> None:
        with self._scope() as session:
            session.execute(delete(m.QueryTemplate))

    def detach_file(self, path: str) -> None:
        with self._scope() as session:
            file_row = session.scalars(
                select(m.SqlFile).where(m.SqlFile.path == path)
            ).one_or_none()
            fingerprints = list(file_row.fingerprints) if file_row is not None else []
            if not fingerprints:
                return
            rows = session.scalars(
                select(m.QueryTemplate).where(m.QueryTemplate.fingerprint.in_(fingerprints))
            )
            for row in rows:
                row.file_paths = [p for p in row.file_paths if p != path]
                row.source_kinds = _source_kinds(row.execution_count, row.file_paths)
                if not row.file_paths and row.execution_count == 0:
                    session.delete(row)
            session.flush()

    def list_sql_files(self) -> list[SqlFileRecord]:
        with self._scope() as session:
            rows = session.scalars(select(m.SqlFile).order_by(m.SqlFile.path))
            return [
                SqlFileRecord(
                    path=row.path,
                    content_hash=row.content_hash,
                    commit_sha=row.commit_sha,
                    statement_count=row.statement_count,
                    parse_failure_count=row.parse_failure_count,
                    fingerprints=list(row.fingerprints),
                    processed_at=row.processed_at,
                )
                for row in rows
            ]

    def upsert_sql_files(self, files: Iterable[SqlFileRecord]) -> None:
        with self._scope() as session:
            for record in files:
                row = session.scalars(
                    select(m.SqlFile).where(m.SqlFile.path == record.path)
                ).one_or_none()
                if row is None:
                    row = m.SqlFile(path=record.path, content_hash=record.content_hash)
                    session.add(row)
                row.content_hash = record.content_hash
                row.commit_sha = record.commit_sha
                row.statement_count = record.statement_count
                row.parse_failure_count = record.parse_failure_count
                row.fingerprints = sorted(set(record.fingerprints))
                row.processed_at = record.processed_at or utcnow()
            session.flush()

    def delete_sql_files(self, paths: Iterable[str]) -> None:
        with self._scope() as session:
            for path in paths:
                self.detach_file(path)
                session.execute(delete(m.SqlFile).where(m.SqlFile.path == path))

    def record_parse_failures(
        self, failures: Iterable[ParseFailureRecord], *, seen_at: datetime
    ) -> None:
        with self._scope() as session:
            for failure in failures:
                row = session.scalars(
                    select(m.ParseFailure).where(
                        m.ParseFailure.sql_hash == failure.sql_hash,
                        m.ParseFailure.source_kind == failure.source_kind,
                        m.ParseFailure.source_ref.is_(None)
                        if failure.source_ref is None
                        else m.ParseFailure.source_ref == failure.source_ref,
                    )
                ).one_or_none()
                if row is None:
                    session.add(
                        m.ParseFailure(
                            sql_hash=failure.sql_hash,
                            dialects_tried=failure.dialects_tried,
                            error_type=failure.error_type,
                            source_kind=failure.source_kind,
                            source_ref=failure.source_ref,
                            count=1,
                            first_seen_at=seen_at,
                            last_seen_at=seen_at,
                        )
                    )
                else:
                    row.count += 1
                    row.last_seen_at = seen_at
                    row.error_type = failure.error_type
                    row.dialects_tried = failure.dialects_tried
            session.flush()

    def count_parse_failures(self) -> int:
        with self._scope() as session:
            return len(session.scalars(select(m.ParseFailure.id)).all())

    # -- derived data ----------------------------------------------------------------

    def replace_join_edges(self, edges: Iterable[JoinEdgeRecord]) -> None:
        with self._scope() as session:
            session.execute(delete(m.JoinEdge))
            for edge in edges:
                session.add(
                    m.JoinEdge(
                        left_table=edge.left_table,
                        left_column=edge.left_column,
                        right_table=edge.right_table,
                        right_column=edge.right_column,
                        join_type=edge.join_type,
                        occurrence_count=edge.occurrence_count,
                        score=edge.score,
                        sources=dict(edge.sources),
                    )
                )
            session.flush()

    def list_join_edges(self) -> list[JoinEdgeRecord]:
        with self._scope() as session:
            rows = session.scalars(
                select(m.JoinEdge).order_by(
                    m.JoinEdge.score.desc(), m.JoinEdge.left_table, m.JoinEdge.right_table
                )
            )
            return [
                JoinEdgeRecord(
                    left_table=row.left_table,
                    left_column=row.left_column,
                    right_table=row.right_table,
                    right_column=row.right_column,
                    join_type=row.join_type,
                    occurrence_count=row.occurrence_count,
                    score=row.score,
                    sources=dict(row.sources),
                )
                for row in rows
            ]

    def replace_lineage_edges(self, edges: Iterable[LineageEdgeRecord]) -> None:
        with self._scope() as session:
            session.execute(delete(m.LineageEdge))
            for edge in edges:
                session.add(
                    m.LineageEdge(
                        upstream_table=edge.upstream_table,
                        downstream_table=edge.downstream_table,
                        source_path=edge.source_path,
                        statement_type=edge.statement_type,
                    )
                )
            session.flush()

    def list_lineage_edges(self) -> list[LineageEdgeRecord]:
        with self._scope() as session:
            rows = session.scalars(
                select(m.LineageEdge).order_by(
                    m.LineageEdge.downstream_table, m.LineageEdge.upstream_table
                )
            )
            return [
                LineageEdgeRecord(
                    upstream_table=row.upstream_table,
                    downstream_table=row.downstream_table,
                    source_path=row.source_path,
                    statement_type=row.statement_type,
                )
                for row in rows
            ]

    def update_usage(self, usages: Iterable[TableUsage], *, reset_missing: bool = True) -> None:
        by_name = {usage.full_name.lower(): usage for usage in usages}
        with self._scope() as session:
            rows = session.scalars(
                select(m.CatalogTable).options(selectinload(m.CatalogTable.columns))
            )
            for row in rows:
                usage = by_name.get(row.full_name)
                if usage is None:
                    if not reset_missing:
                        continue
                    usage = TableUsage(full_name=row.full_name)
                row.use_count = usage.use_count
                row.user_count = usage.user_count
                row.team_count = usage.team_count
                row.last_queried_at = usage.last_queried_at
                for column in row.columns:
                    stats = usage.columns.get(column.name.lower())
                    column.use_count = stats.use_count if stats else 0
                    column.join_key_count = stats.join_key_count if stats else 0
                    column.filter_count = stats.filter_count if stats else 0
                    column.is_join_key = stats.is_join_key if stats else False
                    column.is_filter = stats.is_filter if stats else False
            session.flush()

    # -- table cards -----------------------------------------------------------------

    def get_card(self, table: str) -> CardRecord | None:
        with self._scope() as session:
            row = self._get_card_row(session, table)
            return _card_record(row) if row is not None else None

    def list_cards(self, *, status: str | None = None) -> list[CardRecord]:
        with self._scope() as session:
            query = (
                select(m.TableCard)
                .join(m.CatalogTable)
                .options(selectinload(m.TableCard.table))
                .where(m.CatalogTable.deleted_at.is_(None))
                .order_by(m.CatalogTable.full_name)
            )
            if status is not None:
                query = query.where(m.TableCard.review_status == status)
            return [_card_record(row) for row in session.scalars(query)]

    def save_generated_card(
        self,
        table: str,
        draft: CardDraft,
        *,
        input_hash: str,
        model: str | None,
        generated_at: datetime,
    ) -> CardRecord | None:
        with self._scope() as session:
            table_row = self._get_table_row(session, table.lower())
            if table_row is None:
                return None
            row = table_row.card
            if row is None:
                row = m.TableCard(table=table_row, review_status=CARD_STATUS_AI)
                session.add(row)
            protected = set(row.confirmed_fields or [])
            if row.review_status != CARD_STATUS_AI:
                # A confirmed or rejected card keeps every reviewed field.
                protected.update(CARD_EDITABLE_FIELDS)
            if "purpose" not in protected:
                row.purpose = draft.purpose
            if "key_columns" not in protected:
                row.key_columns = [column.to_dict() for column in draft.key_columns]
            if "common_usage" not in protected:
                row.common_usage = draft.common_usage
            if "example_questions" not in protected:
                row.example_questions = list(draft.example_questions)
            row.evidence = list(draft.evidence)
            row.confidence = draft.overall_confidence
            row.input_hash = input_hash
            row.generated_at = generated_at
            row.model = model
            session.flush()
            return _card_record(row)

    def review_card(
        self,
        table: str,
        *,
        status: str,
        reviewed_by: str,
        reviewed_at: datetime,
        edits: Mapping[str, Any] | None = None,
    ) -> CardRecord | None:
        if status not in CARD_STATUSES:
            msg = f"unknown card status {status!r}"
            raise ValueError(msg)
        with self._scope() as session:
            table_row = self._get_table_row(session, table.lower())
            if table_row is None:
                return None
            row = table_row.card
            if row is None:
                row = m.TableCard(table=table_row, review_status=CARD_STATUS_AI)
                session.add(row)
            confirmed = list(row.confirmed_fields or [])
            for name, value in (edits or {}).items():
                if name not in CARD_EDITABLE_FIELDS:
                    msg = f"card field {name!r} cannot be edited"
                    raise ValueError(msg)
                if name == "key_columns":
                    value = [
                        KeyColumn.from_dict(v).to_dict() if isinstance(v, Mapping) else v
                        for v in value
                    ]
                setattr(row, name, value)
                if name not in confirmed:
                    confirmed.append(name)
            row.confirmed_fields = confirmed
            row.review_status = status
            row.reviewed_by = reviewed_by
            row.reviewed_at = reviewed_at
            session.flush()
            return _card_record(row)

    def set_column_descriptions(self, table: str, descriptions: Mapping[str, str]) -> None:
        with self._scope() as session:
            table_row = self._get_table_row(session, table.lower())
            if table_row is None:
                return
            wanted = {name.lower(): text for name, text in descriptions.items()}
            for column in table_row.columns:
                text = wanted.get(column.name.lower())
                if text is not None:
                    column.ai_description = text
            session.flush()

    @staticmethod
    def _get_card_row(session: Session, table: str) -> m.TableCard | None:
        query = (
            select(m.TableCard)
            .join(m.CatalogTable)
            .options(selectinload(m.TableCard.table))
            .where(m.CatalogTable.full_name == table.lower())
        )
        return session.scalars(query).one_or_none()

    # -- audit -------------------------------------------------------------------------

    def add_audit_entry(self, entry: AuditRecord) -> int:
        with self._scope() as session:
            row = m.AuditLog(
                occurred_at=entry.occurred_at or utcnow(),
                user=entry.user,
                question=entry.question,
                generated_sql=entry.generated_sql,
                validation_status=entry.validation_status,
                sample_confirmed=entry.sample_confirmed,
                sample_executed=entry.sample_executed,
                sample_row_count=entry.sample_row_count,
                details=dict(entry.details),
            )
            session.add(row)
            session.flush()
            return row.id

    def list_audit_entries(self, *, user: str | None = None, limit: int = 100) -> list[AuditRecord]:
        with self._scope() as session:
            query = select(m.AuditLog).order_by(m.AuditLog.id.desc()).limit(limit)
            if user is not None:
                query = query.where(m.AuditLog.user == user)
            return [
                AuditRecord(
                    id=row.id,
                    occurred_at=row.occurred_at,
                    user=row.user,
                    question=row.question,
                    generated_sql=row.generated_sql,
                    validation_status=row.validation_status,
                    sample_confirmed=row.sample_confirmed,
                    sample_executed=row.sample_executed,
                    sample_row_count=row.sample_row_count,
                    details=dict(row.details),
                )
                for row in session.scalars(query)
            ]

    # -- build runs ------------------------------------------------------------------

    def start_build_run(self, run_type: str, *, started_at: datetime) -> int:
        with self._scope() as session:
            row = m.BuildRun(run_type=run_type, status="running", started_at=started_at)
            session.add(row)
            session.flush()
            return row.id

    def finish_build_run(
        self, run_id: int, *, status: str, finished_at: datetime, **fields: Any
    ) -> None:
        with self._scope() as session:
            row = session.get(m.BuildRun, run_id)
            if row is None:
                msg = f"unknown build run {run_id}"
                raise KeyError(msg)
            row.status = status
            row.finished_at = finished_at
            for key, value in fields.items():
                if not hasattr(row, key):
                    msg = f"unknown build_run field {key!r}"
                    raise AttributeError(msg)
                setattr(row, key, value)
            session.flush()

    def last_successful_run(self) -> BuildRunRecord | None:
        with self._scope() as session:
            row = session.scalars(
                select(m.BuildRun)
                .where(m.BuildRun.status == "succeeded")
                .order_by(m.BuildRun.finished_at.desc(), m.BuildRun.id.desc())
            ).first()
            return _build_run_record(row) if row is not None else None

    def list_build_runs(self) -> list[BuildRunRecord]:
        with self._scope() as session:
            rows = session.scalars(select(m.BuildRun).order_by(m.BuildRun.id))
            return [_build_run_record(row) for row in rows]


# -- helpers ---------------------------------------------------------------------------


def _sync_columns(row: m.CatalogTable, record: TableRecord) -> None:
    existing = {column.name.lower(): column for column in row.columns}
    wanted = {column.name.lower(): column for column in record.columns}
    for key, column in list(existing.items()):
        if key not in wanted:
            row.columns.remove(column)
    for key, wanted_column in wanted.items():
        current = existing.get(key)
        if current is None:
            row.columns.append(
                m.CatalogColumn(
                    name=wanted_column.name,
                    ordinal_position=wanted_column.ordinal_position,
                    data_type=wanted_column.data_type,
                    comment=wanted_column.comment,
                )
            )
        else:
            current.ordinal_position = wanted_column.ordinal_position
            current.data_type = wanted_column.data_type
            current.comment = wanted_column.comment


def _table_state(row: m.CatalogTable) -> TableState:
    return TableState(
        full_name=row.full_name,
        catalog=row.catalog,
        schema_name=row.schema_name,
        table_name=row.table_name,
        structure_hash=row.structure_hash,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        table_type=row.table_type,
        comment=row.comment,
        owner_team=row.owner_team,
        owner_source=row.owner_source,
        last_queried_at=row.last_queried_at,
        use_count=row.use_count,
        user_count=row.user_count,
        team_count=row.team_count,
        deleted_at=row.deleted_at,
        columns=[
            ColumnState(
                name=column.name,
                ordinal_position=column.ordinal_position,
                data_type=column.data_type,
                comment=column.comment,
                ai_description=column.ai_description,
                use_count=column.use_count,
                join_key_count=column.join_key_count,
                filter_count=column.filter_count,
                is_join_key=column.is_join_key,
                is_filter=column.is_filter,
                pii_flag=column.pii_flag,
            )
            for column in sorted(row.columns, key=lambda c: c.ordinal_position)
        ],
    )


def _template_record(row: m.QueryTemplate) -> TemplateRecord:
    return TemplateRecord(
        fingerprint=row.fingerprint,
        normalized_sql=row.normalized_sql,
        dialect=row.dialect,
        statement_type=row.statement_type,
        tables_read=list(row.tables_read),
        tables_written=list(row.tables_written),
        parsed=dict(row.parsed),
        execution_count=row.execution_count,
        users=list(row.users),
        teams=list(row.teams),
        file_paths=list(row.file_paths),
        first_seen_at=row.first_seen_at,
        last_executed_at=row.last_executed_at,
    )


def _card_record(row: m.TableCard) -> CardRecord:
    return CardRecord(
        table=row.table.full_name,
        purpose=row.purpose,
        key_columns=[KeyColumn.from_dict(c) for c in (row.key_columns or [])],
        common_usage=row.common_usage,
        example_questions=list(row.example_questions or []),
        evidence=list(row.evidence or []),
        confidence=row.confidence,
        review_status=row.review_status,
        reviewed_by=row.reviewed_by,
        reviewed_at=row.reviewed_at,
        confirmed_fields=list(row.confirmed_fields or []),
        input_hash=row.input_hash,
        generated_at=row.generated_at,
        model=row.model,
    )


def _build_run_record(row: m.BuildRun) -> BuildRunRecord:
    return BuildRunRecord(
        id=row.id,
        run_type=row.run_type,
        status=row.status,
        started_at=row.started_at,
        finished_at=row.finished_at,
        history_processed_until=row.history_processed_until,
        repo_commit_sha=row.repo_commit_sha,
        tables_added=row.tables_added,
        tables_changed=row.tables_changed,
        tables_removed=row.tables_removed,
        queries_processed=row.queries_processed,
        files_processed=row.files_processed,
        parse_failures=row.parse_failures,
        summary=dict(row.summary),
    )


def _source_kinds(execution_count: int, file_paths: list[str]) -> list[str]:
    kinds: list[str] = []
    if execution_count > 0:
        kinds.append("history")
    if file_paths:
        kinds.append("file")
    return kinds


def _max_optional(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)
