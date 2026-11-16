"""The ``build`` / ``refresh`` pipeline.

Both commands share one flow; ``refresh`` merely narrows the inputs to what
changed since the last successful run (new history, changed files, and the
metadata diff) and merges into the stored templates instead of replacing them.

Everything is computed in memory first; the database is written in a single
transaction at the end, and not at all with ``dry_run=True``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from data_navigator.analysis.join_graph import compute_join_graph
from data_navigator.analysis.lineage import compute_lineage
from data_navigator.analysis.ownership import infer_owner
from data_navigator.analysis.usage import compute_usage
from data_navigator.catalog.records import (
    ParseFailureRecord,
    SqlFileRecord,
    TableDiff,
    TableRecord,
    TableState,
    TemplateRecord,
)
from data_navigator.catalog.repository import CatalogRepository, SchemaMap
from data_navigator.catalog.types import utcnow
from data_navigator.config import Settings
from data_navigator.sources.factory import Sources
from data_navigator.sources.query_history import QueryHistoryRecord
from data_navigator.sources.sql_files import SqlFile
from data_navigator.sqlproc.fingerprint import fingerprint
from data_navigator.sqlproc.normalize import (
    NormalizationError,
    NormalizedSQL,
    UnsupportedStatementError,
    normalize,
)
from data_navigator.sqlproc.parse import TableResolver, parse
from data_navigator.sqlproc.template import TemplateProcessor

log = logging.getLogger(__name__)


@dataclass
class BuildSummary:
    run_type: str
    dry_run: bool
    started_at: datetime
    finished_at: datetime | None = None
    run_id: int | None = None
    tables_seen: int = 0
    tables_added: list[str] = field(default_factory=list)
    tables_changed: list[str] = field(default_factory=list)
    tables_removed: list[str] = field(default_factory=list)
    tables_unchanged: int = 0
    owners_inferred: int = 0
    history_since: datetime | None = None
    history_until: datetime | None = None
    queries_processed: int = 0
    queries_skipped: int = 0
    query_parse_failures: int = 0
    repo_commit: str | None = None
    files_processed: int = 0
    files_unchanged: int = 0
    files_removed: int = 0
    statements_processed: int = 0
    statements_skipped: int = 0
    file_parse_failures: int = 0
    templates_new: int = 0
    templates_total: int = 0
    join_edges: int = 0
    lineage_edges: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def parse_failures(self) -> int:
        return self.query_parse_failures + self.file_parse_failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_type": self.run_type,
            "dry_run": self.dry_run,
            "tables_seen": self.tables_seen,
            "tables_added": self.tables_added,
            "tables_changed": self.tables_changed,
            "tables_removed": self.tables_removed,
            "tables_unchanged": self.tables_unchanged,
            "owners_inferred": self.owners_inferred,
            "history_since": self.history_since.isoformat() if self.history_since else None,
            "history_until": self.history_until.isoformat() if self.history_until else None,
            "queries_processed": self.queries_processed,
            "queries_skipped": self.queries_skipped,
            "query_parse_failures": self.query_parse_failures,
            "repo_commit": self.repo_commit,
            "files_processed": self.files_processed,
            "files_unchanged": self.files_unchanged,
            "files_removed": self.files_removed,
            "statements_processed": self.statements_processed,
            "statements_skipped": self.statements_skipped,
            "file_parse_failures": self.file_parse_failures,
            "templates_new": self.templates_new,
            "templates_total": self.templates_total,
            "join_edges": self.join_edges,
            "lineage_edges": self.lineage_edges,
            "warnings": self.warnings,
        }

    def render(self) -> str:
        mode = " (dry run, nothing written)" if self.dry_run else ""
        tables_line = (
            f"  tables: {self.tables_seen} seen, {len(self.tables_added)} added, "
            f"{len(self.tables_changed)} changed, {len(self.tables_removed)} removed, "
            f"{self.tables_unchanged} unchanged; owners inferred: {self.owners_inferred}"
        )
        lines = [f"{self.run_type}{mode}", tables_line]
        for label, names in (
            ("added", self.tables_added),
            ("changed", self.tables_changed),
            ("removed", self.tables_removed),
        ):
            if names:
                lines.append(f"    {label}: {', '.join(names)}")
        window = ""
        if self.history_since or self.history_until:
            since = self.history_since.isoformat() if self.history_since else "-"
            until = self.history_until.isoformat() if self.history_until else "-"
            window = f" (window {since} .. {until})"
        lines.append(
            f"  history: {self.queries_processed} queries processed, "
            f"{self.queries_skipped} skipped, {self.query_parse_failures} parse failures{window}"
        )
        commit = f" @ {self.repo_commit[:12]}" if self.repo_commit else ""
        lines.append(
            f"  files{commit}: {self.files_processed} processed, {self.files_unchanged} unchanged, "
            f"{self.files_removed} removed; {self.statements_processed} statements, "
            f"{self.statements_skipped} skipped, {self.file_parse_failures} parse failures"
        )
        lines.append(
            f"  patterns: {self.templates_new} new, {self.templates_total} total; "
            f"join edges: {self.join_edges}; lineage edges: {self.lineage_edges}"
        )
        for warning in self.warnings:
            lines.append(f"  warning: {warning}")
        return "\n".join(lines)


@dataclass
class _Pending:
    """A template accumulated during one run, before it is merged into storage."""

    record: TemplateRecord
    normalized: NormalizedSQL
    session_catalog: str | None = None
    session_schema: str | None = None


class CatalogBuilder:
    def __init__(
        self,
        settings: Settings,
        repository: CatalogRepository,
        sources: Sources,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._settings = settings
        self._repo = repository
        self._sources = sources
        self._now = now
        self._templates = TemplateProcessor(settings.template)

    def build(self, *, dry_run: bool = False) -> BuildSummary:
        return self._run("build", dry_run=dry_run, tables=None)

    def refresh(
        self, *, dry_run: bool = False, tables: Sequence[str] | None = None
    ) -> BuildSummary:
        return self._run("refresh", dry_run=dry_run, tables=tables)

    # -- the flow --------------------------------------------------------------------

    def _run(self, run_type: str, *, dry_run: bool, tables: Sequence[str] | None) -> BuildSummary:
        started = self._now()
        summary = BuildSummary(run_type=run_type, dry_run=dry_run, started_at=started)
        incremental = run_type == "refresh"
        last_run = self._repo.last_successful_run() if incremental else None
        if incremental and last_run is None:
            summary.warnings.append("no previous successful run; refresh behaves like a full build")
        table_filter = {name.lower() for name in tables} if tables else None

        # 1. Metadata -------------------------------------------------------------
        snapshot = self._sources.metadata.fetch()
        seen = [t for t in snapshot.tables if table_filter is None or t.full_name in table_filter]
        summary.tables_seen = len(seen)
        existing = {t.full_name: t for t in self._repo.list_tables(include_deleted=True)}
        diff = _diff_tables(seen, existing, table_filter)
        summary.tables_added = diff.added
        summary.tables_changed = diff.changed
        summary.tables_removed = diff.removed
        summary.tables_unchanged = len(diff.unchanged)

        schema_map, known_tables = _schema_from(seen, existing.values())
        resolver = TableResolver(
            known_tables,
            default_catalog=self._settings.sql_files.default_catalog,
            default_schema=self._settings.sql_files.default_schema,
            placeholder=self._templates.placeholder,
        )
        owners = {
            t.full_name: owner
            for t in seen
            if (
                owner := infer_owner(
                    full_name=t.full_name,
                    schema_name=t.schema_name,
                    rules=self._settings.analysis.ownership_rules,
                )
            )
        }
        summary.owners_inferred = len(owners)

        # 2. Query history ----------------------------------------------------------
        pending: dict[str, _Pending] = {}
        failures: list[ParseFailureRecord] = []
        since = (
            last_run.history_processed_until
            if last_run is not None and last_run.history_processed_until is not None
            else started - timedelta(days=self._settings.query_history.window_days)
        )
        summary.history_since, summary.history_until = since, started
        self._ingest_history(
            self._sources.query_history.fetch(since=since, until=started),
            pending,
            failures,
            summary,
            seen_at=started,
        )

        # 3. SQL files ----------------------------------------------------------------
        file_records, removed_paths, changed_paths = self._ingest_files(
            pending, failures, summary, last_run, incremental=incremental, seen_at=started
        )

        # 4. Parse every new pattern against the current schema ---------------------------
        new_templates: list[TemplateRecord] = []
        for item in pending.values():
            per_statement = resolver.with_defaults(item.session_catalog, item.session_schema)
            try:
                parsed = parse(item.normalized, schema=schema_map, resolver=per_statement)
            except Exception as error:
                log.warning("analysis failed for a pattern (%s)", type(error).__name__)
                failures.append(
                    ParseFailureRecord(
                        sql_hash=item.record.fingerprint,
                        dialects_tried=item.normalized.dialect,
                        error_type=f"Analysis:{type(error).__name__}",
                        source_kind="file" if item.record.file_paths else "history",
                    )
                )
                continue
            item.record.parsed = parsed.to_dict()
            item.record.tables_read = parsed.tables_read
            item.record.tables_written = parsed.tables_written
            item.record.statement_type = parsed.statement_type
            new_templates.append(item.record)
        summary.templates_new = len(new_templates)

        # 5. Analysis over stored + new patterns ---------------------------------------
        stored = [] if not incremental else self._repo.list_templates()
        stored_by_fp = {t.fingerprint: t for t in stored}
        for path in changed_paths | set(removed_paths):
            _forget_file(stored_by_fp, path)
        all_templates = _merge_templates(stored_by_fp.values(), new_templates)
        summary.templates_total = len(all_templates)
        usage = compute_usage(
            all_templates.values(),
            join_key_min_count=self._settings.analysis.join_key_min_count,
            filter_min_count=self._settings.analysis.filter_min_count,
        )
        join_edges = compute_join_graph(all_templates.values(), self._settings.analysis)
        lineage = compute_lineage(all_templates.values())
        summary.join_edges = len(join_edges)
        summary.lineage_edges = len(lineage)

        # 6. Persist ---------------------------------------------------------------------
        if dry_run:
            summary.finished_at = self._now()
            return summary
        try:
            with self._repo.transaction():
                run_id = self._repo.start_build_run(run_type, started_at=started)
                summary.run_id = run_id
                self._repo.upsert_tables(seen, seen_at=started)
                self._repo.mark_tables_deleted(diff.removed, at=started)
                for name, team in owners.items():
                    self._repo.set_owner(name, team, source="inferred")
                if not incremental:
                    self._repo.clear_templates()
                    self._repo.delete_sql_files([f.path for f in self._repo.list_sql_files()])
                else:
                    for path in changed_paths:
                        self._repo.detach_file(path)
                    self._repo.delete_sql_files(removed_paths)
                self._repo.upsert_templates(new_templates)
                self._repo.upsert_sql_files(file_records)
                self._repo.record_parse_failures(failures, seen_at=started)
                self._repo.update_usage(usage.values(), reset_missing=True)
                self._repo.replace_join_edges(join_edges)
                self._repo.replace_lineage_edges(lineage)
                finished = self._now()
                summary.finished_at = finished
                self._repo.finish_build_run(
                    run_id,
                    status="succeeded",
                    finished_at=finished,
                    history_processed_until=started,
                    repo_commit_sha=summary.repo_commit,
                    tables_added=len(diff.added),
                    tables_changed=len(diff.changed),
                    tables_removed=len(diff.removed),
                    queries_processed=summary.queries_processed,
                    files_processed=summary.files_processed,
                    parse_failures=summary.parse_failures,
                    summary=summary.to_dict(),
                )
        except Exception as error:
            log.error("%s failed: %s", run_type, type(error).__name__)
            failed_at = self._now()
            with self._repo.transaction():
                run_id = self._repo.start_build_run(run_type, started_at=started)
                self._repo.finish_build_run(
                    run_id,
                    status="failed",
                    finished_at=failed_at,
                    summary={"error": type(error).__name__},
                )
            raise
        return summary

    # -- ingestion helpers -------------------------------------------------------------

    def _ingest_history(
        self,
        records: Iterable[QueryHistoryRecord],
        pending: dict[str, _Pending],
        failures: list[ParseFailureRecord],
        summary: BuildSummary,
        *,
        seen_at: datetime,
    ) -> None:
        prefixes = tuple(p.upper() for p in self._settings.query_history.ignored_prefixes)
        for record in records:
            text = record.query_text.strip()
            if not text or text.upper().startswith(prefixes):
                summary.queries_skipped += 1
                continue
            try:
                normalized = normalize(text, placeholder=self._templates.placeholder)
            except UnsupportedStatementError:
                summary.queries_skipped += 1
                continue
            except NormalizationError as error:
                summary.query_parse_failures += 1
                failures.append(
                    ParseFailureRecord(
                        sql_hash=error.sql_hash,
                        dialects_tried=",".join(error.dialects_tried),
                        error_type=error.error_type,
                        source_kind="history",
                    )
                )
                continue
            summary.queries_processed += 1
            key = fingerprint(normalized)
            team = self._sources.teams.team_for_user(record.user)
            item = pending.get(key)
            if item is None:
                item = _Pending(
                    record=TemplateRecord(
                        fingerprint=key,
                        normalized_sql=normalized.text,
                        dialect=normalized.dialect,
                        statement_type=normalized.statement_type,
                        first_seen_at=record.started_at,
                    ),
                    normalized=normalized,
                    session_catalog=record.catalog,
                    session_schema=record.schema,
                )
                pending[key] = item
            template = item.record
            template.execution_count += 1
            if record.user and record.user not in template.users:
                template.users.append(record.user)
            if team and team not in template.teams:
                template.teams.append(team)
            template.last_executed_at = _max_dt(
                template.last_executed_at, record.ended_at or record.started_at
            )
            template.first_seen_at = _min_dt(template.first_seen_at, record.started_at) or seen_at

    def _ingest_files(
        self,
        pending: dict[str, _Pending],
        failures: list[ParseFailureRecord],
        summary: BuildSummary,
        last_run: Any,
        *,
        incremental: bool,
        seen_at: datetime,
    ) -> tuple[list[SqlFileRecord], list[str], set[str]]:
        source = self._sources.sql_files
        try:
            summary.repo_commit = source.sync()
        except Exception as error:
            summary.warnings.append(f"sql files unavailable: {type(error).__name__}")
            log.warning("sql file source unavailable: %s", type(error).__name__)
            return [], [], set()
        since_commit = last_run.repo_commit_sha if (incremental and last_run is not None) else None
        listing = source.list_files(since_commit=since_commit)
        if listing.head_sha:
            summary.repo_commit = listing.head_sha
        stored = {f.path: f for f in self._repo.list_sql_files()} if incremental else {}
        removed = list(listing.removed_paths)
        if incremental and not listing.incremental:
            listed = {f.path for f in listing.files}
            removed.extend(sorted(p for p in stored if p not in listed))
        summary.files_removed = len(removed)

        records: list[SqlFileRecord] = []
        changed: set[str] = set()
        for sql_file in listing.files:
            previous = stored.get(sql_file.path)
            if previous is not None and previous.content_hash == sql_file.content_hash:
                summary.files_unchanged += 1
                continue
            if previous is not None:
                changed.add(sql_file.path)
            records.append(self._ingest_file(sql_file, pending, failures, summary, seen_at=seen_at))
        summary.files_processed = len(records)
        return records, removed, changed

    def _ingest_file(
        self,
        sql_file: SqlFile,
        pending: dict[str, _Pending],
        failures: list[ParseFailureRecord],
        summary: BuildSummary,
        *,
        seen_at: datetime,
    ) -> SqlFileRecord:
        rendered = self._templates.render(sql_file.content)
        statements = self._templates.split_statements(rendered.text)
        record = SqlFileRecord(
            path=sql_file.path,
            content_hash=sql_file.content_hash,
            commit_sha=sql_file.commit_sha,
            statement_count=len(statements),
            processed_at=seen_at,
        )
        for statement in statements:
            try:
                normalized = normalize(statement, placeholder=self._templates.placeholder)
            except UnsupportedStatementError:
                summary.statements_skipped += 1
                continue
            except NormalizationError as error:
                summary.file_parse_failures += 1
                record.parse_failure_count += 1
                failures.append(
                    ParseFailureRecord(
                        sql_hash=error.sql_hash,
                        dialects_tried=",".join(error.dialects_tried),
                        error_type=error.error_type,
                        source_kind="file",
                        source_ref=sql_file.path,
                    )
                )
                continue
            summary.statements_processed += 1
            key = fingerprint(normalized)
            item = pending.get(key)
            if item is None:
                item = _Pending(
                    record=TemplateRecord(
                        fingerprint=key,
                        normalized_sql=normalized.text,
                        dialect=normalized.dialect,
                        statement_type=normalized.statement_type,
                        first_seen_at=seen_at,
                    ),
                    normalized=normalized,
                )
                pending[key] = item
            if sql_file.path not in item.record.file_paths:
                item.record.file_paths.append(sql_file.path)
            if key not in record.fingerprints:
                record.fingerprints.append(key)
        return record


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _diff_tables(
    seen: Sequence[TableRecord],
    existing: dict[str, TableState],
    table_filter: set[str] | None,
) -> TableDiff:
    diff = TableDiff()
    seen_names = set()
    for record in seen:
        seen_names.add(record.full_name)
        current = existing.get(record.full_name)
        if current is None or current.is_deleted:
            diff.added.append(record.full_name)
        elif current.structure_hash != record.structure_hash:
            diff.changed.append(record.full_name)
        else:
            diff.unchanged.append(record.full_name)
    for name, state in existing.items():
        if state.is_deleted or name in seen_names:
            continue
        if table_filter is not None and name not in table_filter:
            continue
        diff.removed.append(name)
    diff.added.sort()
    diff.changed.sort()
    diff.removed.sort()
    return diff


def _schema_from(
    seen: Sequence[TableRecord], existing: Iterable[TableState]
) -> tuple[SchemaMap, list[str]]:
    schema: SchemaMap = {}
    known: list[str] = []
    for state in existing:
        if state.is_deleted:
            continue
        schema.setdefault(state.catalog, {}).setdefault(state.schema_name, {})[state.table_name] = {
            c.name: c.data_type for c in state.columns
        }
        known.append(state.full_name)
    for record in seen:
        schema.setdefault(record.catalog, {}).setdefault(record.schema_name, {})[
            record.table_name
        ] = {c.name: c.data_type for c in record.columns}
        if record.full_name not in known:
            known.append(record.full_name)
    return schema, known


def _forget_file(templates: dict[str, TemplateRecord], path: str) -> None:
    for key in list(templates):
        template = templates[key]
        if path in template.file_paths:
            template.file_paths = [p for p in template.file_paths if p != path]
            if not template.file_paths and template.execution_count == 0:
                del templates[key]


def _merge_templates(
    stored: Iterable[TemplateRecord], new: Iterable[TemplateRecord]
) -> dict[str, TemplateRecord]:
    merged: dict[str, TemplateRecord] = {}
    for template in stored:
        merged[template.fingerprint] = TemplateRecord(
            fingerprint=template.fingerprint,
            normalized_sql=template.normalized_sql,
            dialect=template.dialect,
            statement_type=template.statement_type,
            tables_read=list(template.tables_read),
            tables_written=list(template.tables_written),
            parsed=dict(template.parsed),
            execution_count=template.execution_count,
            users=list(template.users),
            teams=list(template.teams),
            file_paths=list(template.file_paths),
            first_seen_at=template.first_seen_at,
            last_executed_at=template.last_executed_at,
        )
    for template in new:
        current = merged.get(template.fingerprint)
        if current is None:
            merged[template.fingerprint] = template
            continue
        current.normalized_sql = template.normalized_sql
        current.parsed = template.parsed
        current.tables_read = template.tables_read
        current.tables_written = template.tables_written
        current.statement_type = template.statement_type
        current.execution_count += template.execution_count
        current.users = sorted(set(current.users) | set(template.users))
        current.teams = sorted(set(current.teams) | set(template.teams))
        current.file_paths = sorted(set(current.file_paths) | set(template.file_paths))
        current.first_seen_at = _min_dt(current.first_seen_at, template.first_seen_at)
        current.last_executed_at = _max_dt(current.last_executed_at, template.last_executed_at)
    return merged


def _max_dt(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _min_dt(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)
