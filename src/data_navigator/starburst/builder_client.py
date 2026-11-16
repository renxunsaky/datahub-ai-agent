"""Catalog-building client: dedicated user, whitelisted statements only."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from data_navigator.config import Secrets, Settings
from data_navigator.sqlproc.statements import DEFAULT_DIALECT
from data_navigator.starburst.connection import (
    ConnectionFactory,
    Credentials,
    make_connection_factory,
)
from data_navigator.starburst.errors import MappedError, map_exception
from data_navigator.starburst.whitelist import BuilderPolicyError, check_builder_statement

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]


class BuilderQueryError(RuntimeError):
    """A whitelisted statement failed on the server."""

    def __init__(self, mapped: MappedError) -> None:
        super().__init__(f"{mapped.status}: {mapped.message}")
        self.mapped = mapped


class BuilderClient:
    """Executes whitelisted metadata/history queries as the dedicated builder user."""

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        *,
        allowed_tables: Iterable[str] = (),
        dialect: str = DEFAULT_DIALECT,
    ) -> None:
        self._connection_factory = connection_factory
        self._allowed_tables = tuple(allowed_tables)
        self._dialect = dialect

    @property
    def allowed_tables(self) -> tuple[str, ...]:
        return self._allowed_tables

    def check(self, sql: str) -> None:
        """Raise :class:`BuilderPolicyError` unless *sql* is whitelisted."""
        check_builder_statement(sql, allowed_tables=self._allowed_tables, dialect=self._dialect)

    def query(self, sql: str) -> QueryResult:
        """Run a whitelisted statement and return all rows."""
        self.check(sql)  # raises before any connection is opened
        connection = self._connection_factory()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(sql)
                rows = [tuple(row) for row in cursor.fetchall()]
                columns = [str(col[0]) for col in (cursor.description or [])]
            finally:
                cursor.close()
        except BuilderPolicyError:
            raise
        except Exception as error:
            mapped = map_exception(error)
            log.warning("builder statement failed: %s (%s)", mapped.status, mapped.error_name)
            raise BuilderQueryError(mapped) from error
        finally:
            connection.close()
        return QueryResult(columns=columns, rows=rows)


def builder_allowed_tables(settings: Settings) -> Sequence[str]:
    """Tables outside ``information_schema``/``system.metadata`` the builder may read."""
    table = settings.query_history.table
    return (table,) if table else ()


def build_builder_client(settings: Settings, secrets: Secrets) -> BuilderClient:
    """Wire a :class:`BuilderClient` from configuration and secrets."""
    credentials = Credentials(
        user=settings.starburst.builder.user,
        password=secrets.builder_password,
        jwt=None,
    )
    factory = make_connection_factory(
        settings.starburst.connection, credentials, source_suffix="builder"
    )
    return BuilderClient(factory, allowed_tables=builder_allowed_tables(settings))
