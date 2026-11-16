"""End-user client: the person's own credentials, ``EXPLAIN`` and samples only.

Every statement goes through the read-only guard first. Samples are wrapped in a
row limit that comes from configuration (never from the model), and run with the
configured execution-time / scan-size session properties.

Sample rows are returned to the caller and never logged here.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp

from data_navigator.catalog.repository import SchemaMap
from data_navigator.config import SampleSettings, Secrets, Settings
from data_navigator.sqlproc.readonly_guard import (
    apply_row_limit,
    check_readonly_expression,
    readonly_guard,
)
from data_navigator.sqlproc.statements import (
    DEFAULT_DIALECT,
    StatementError,
    parse_single_statement,
)
from data_navigator.starburst.connection import (
    ConnectionFactory,
    Credentials,
    make_connection_factory,
)
from data_navigator.starburst.errors import Status, map_exception

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExplainResult:
    status: Status
    plan: str | None = None
    message: str | None = None
    error_name: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class SampleResult:
    status: Status
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    message: str | None = None
    error_name: str | None = None
    executed_sql: str | None = None
    row_limit: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def row_count(self) -> int:
        return len(self.rows)


def sql_digest(sql: str) -> str:
    """Stable identifier for a statement that is safe to log (no SQL text)."""
    return hashlib.sha256(sql.strip().encode("utf-8")).hexdigest()[:16]


class EndUserClient:
    """Executes ``EXPLAIN`` and row-limited samples with the end user's identity."""

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        *,
        max_rows: int,
        dialect: str = DEFAULT_DIALECT,
    ) -> None:
        if max_rows < 1:
            msg = "max_rows must be at least 1"
            raise ValueError(msg)
        self._connection_factory = connection_factory
        self._max_rows = max_rows
        self._dialect = dialect

    @property
    def max_rows(self) -> int:
        return self._max_rows

    def explain(self, sql: str, *, validate_only: bool = False) -> ExplainResult:
        """Run ``EXPLAIN`` (or ``EXPLAIN (TYPE VALIDATE)``) for a read-only statement."""
        guard = readonly_guard(sql, self._dialect)
        if guard.rejected:
            return ExplainResult(status="rejected", message=guard.reason)
        statement = parse_single_statement(sql, self._dialect)
        prefix = "EXPLAIN (TYPE VALIDATE) " if validate_only else "EXPLAIN "
        explain_sql = prefix + statement.sql(dialect=self._dialect)
        try:
            _columns, rows = self._execute(explain_sql)
        except Exception as error:
            mapped = map_exception(error)
            log.info("explain %s -> %s (%s)", sql_digest(sql), mapped.status, mapped.error_name)
            return ExplainResult(
                status=mapped.status, message=mapped.message, error_name=mapped.error_name
            )
        plan = "\n".join(str(row[0]) for row in rows if row) if rows else ""
        log.info("explain %s -> ok", sql_digest(sql))
        return ExplainResult(status="ok", plan=plan)

    def sample(self, sql: str, max_rows: int | None = None) -> SampleResult:
        """Run a read-only statement capped at ``min(max_rows, configured cap)`` rows."""
        limit = self._effective_limit(max_rows)
        guard = readonly_guard(sql, self._dialect)
        if guard.rejected:
            return SampleResult(status="rejected", message=guard.reason, row_limit=limit)
        statement = parse_single_statement(sql, self._dialect)
        limited = apply_row_limit(statement, limit)
        executed_sql = limited.sql(dialect=self._dialect)
        # Defence in depth: the statement actually sent must still pass the guard and
        # must carry a limit no larger than the cap.
        limit = self._assert_limited(executed_sql, limit)
        try:
            columns, rows = self._execute(executed_sql)
        except Exception as error:
            mapped = map_exception(error)
            log.info("sample %s -> %s (%s)", sql_digest(sql), mapped.status, mapped.error_name)
            return SampleResult(
                status=mapped.status,
                message=mapped.message,
                error_name=mapped.error_name,
                executed_sql=executed_sql,
                row_limit=limit,
            )
        rows = rows[:limit]
        log.info("sample %s -> ok, %d row(s)", sql_digest(sql), len(rows))
        return SampleResult(
            status="ok", columns=columns, rows=rows, executed_sql=executed_sql, row_limit=limit
        )

    # -- internals -----------------------------------------------------------------

    def _effective_limit(self, requested: int | None) -> int:
        if requested is None:
            return self._max_rows
        if requested < 1:
            msg = "max_rows must be at least 1"
            raise ValueError(msg)
        return min(requested, self._max_rows)

    def _assert_limited(self, executed_sql: str, limit: int) -> int:
        """Return the literal row limit of *executed_sql*, or fail if it exceeds *limit*."""
        try:
            statement = parse_single_statement(executed_sql, self._dialect)
        except StatementError as error:  # pragma: no cover - would be a bug in apply_row_limit
            msg = "limited statement is not parseable"
            raise RuntimeError(msg) from error
        guard = check_readonly_expression(statement)
        limit_node = statement.args.get("limit")
        value = limit_node.expression if isinstance(limit_node, exp.Limit) else None
        if (
            guard.rejected
            or not isinstance(value, exp.Literal)
            or value.is_string
            or not value.this.isdigit()
            or int(value.this) > limit
        ):
            msg = "row limit was not applied; refusing to execute"
            raise RuntimeError(msg)
        return int(value.this)

    def _execute(self, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        connection = self._connection_factory()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(sql)
                rows = [tuple(row) for row in cursor.fetchall()]
                columns = [str(col[0]) for col in (cursor.description or [])]
            finally:
                cursor.close()
        finally:
            connection.close()
        return columns, rows


def sample_session_properties(samples: SampleSettings) -> Mapping[str, str]:
    """Session properties applied to every end-user statement."""
    properties = dict(samples.session_properties)
    properties["query_max_execution_time"] = samples.query_max_execution_time
    return properties


def build_end_user_client(
    settings: Settings,
    secrets: Secrets,
    *,
    user: str | None = None,
    schema_map: SchemaMap | None = None,
) -> EndUserClient:
    """Wire an :class:`EndUserClient` for the identity supplied by the harness.

    *user* defaults to ``DN_END_USER_USER``; the model never sees or sets it.
    With ``starburst.end_user.source: fixture`` the statements are simulated
    against *schema_map* (the catalog) instead of a cluster.
    """
    identity = user or secrets.end_user_user
    if not identity:
        msg = "end-user identity missing: set DN_END_USER_USER"
        raise RuntimeError(msg)
    factory: ConnectionFactory
    if settings.starburst.end_user.source == "fixture":
        from data_navigator.starburst.fixture_connection import make_fixture_connection_factory

        factory = make_fixture_connection_factory(
            schema_map or {}, user=identity, access=settings.starburst.end_user.fixture_access
        )
        log.info("end-user statements are simulated (starburst.end_user.source: fixture)")
    else:
        credentials = Credentials(
            user=identity, password=secrets.end_user_password, jwt=secrets.end_user_jwt
        )
        factory = make_connection_factory(
            settings.starburst.connection,
            credentials,
            source_suffix="end-user",
            session_properties=sample_session_properties(settings.samples),
        )
    return EndUserClient(factory, max_rows=settings.samples.max_rows)
