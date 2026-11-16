"""A simulated end-user connection for local runs without a Starburst cluster.

It implements the small DB-API subset the :class:`EndUserClient` uses and
answers ``EXPLAIN`` / ``SELECT`` statements by analysing them against the
catalog schema:

* unknown tables / columns raise the same ``TrinoUserError`` codes the server
  would (``TABLE_NOT_FOUND``, ``COLUMN_NOT_FOUND``, ``SYNTAX_ERROR``);
* access is decided by a static per-user list of glob patterns
  (``starburst.end_user.fixture_access``) and denied with ``PERMISSION_DENIED``;
* ``SELECT`` returns synthetic placeholder rows (never real data).

So the whole validate -> candidates -> sample flow can be exercised locally; the
real cluster is still required for the final acceptance (see docs).
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping, Sequence
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify
from sqlglot.schema import MappingSchema
from trino.exceptions import TrinoUserError

from data_navigator.catalog.repository import SchemaMap
from data_navigator.starburst.connection import ConnectionFactory, ConnectionLike, CursorLike

_EXPLAIN = re.compile(r"^\s*EXPLAIN\s*(\([^)]*\))?\s*", re.IGNORECASE)


def _user_error(name: str, message: str) -> TrinoUserError:
    return TrinoUserError(
        {"errorName": name, "errorType": "USER_ERROR", "message": message, "errorCode": 1},
        "fixture_query",
    )


class FixtureAccessPolicy:
    """``user -> [glob patterns over catalog.schema.table]``."""

    def __init__(self, rules: Mapping[str, Sequence[str]]) -> None:
        self._rules = {user.lower(): list(patterns) for user, patterns in rules.items()}

    def allows(self, user: str, table: str) -> bool:
        patterns = self._rules.get(user.lower(), [])
        return any(fnmatch.fnmatchcase(table.lower(), p.lower()) for p in patterns)


class FixtureCursor:
    def __init__(self, connection: FixtureConnection) -> None:
        self._connection = connection
        self.description: Sequence[Sequence[Any]] | None = None
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, operation: str, params: Any = None) -> None:
        columns, rows = self._connection.run(operation)
        self.description = [(name, "varchar") for name in columns]
        self._rows = rows

    def fetchall(self) -> Sequence[Sequence[Any]]:
        return list(self._rows)

    def close(self) -> None:
        self._rows = []


class FixtureConnection:
    def __init__(
        self,
        schema_map: SchemaMap,
        *,
        user: str,
        policy: FixtureAccessPolicy,
        dialect: str = "trino",
        default_rows: int = 3,
    ) -> None:
        self._schema = MappingSchema(_as_schema_dict(schema_map), dialect=dialect)
        self._tables = {
            f"{c}.{s}.{t}".lower()
            for c, schemas in schema_map.items()
            for s, tables in schemas.items()
            for t in tables
        }
        self._user = user
        self._policy = policy
        self._dialect = dialect
        self._default_rows = default_rows
        self.executed: list[str] = []
        self.closed = False

    def cursor(self) -> CursorLike:
        return FixtureCursor(self)

    def close(self) -> None:
        self.closed = True

    # -- simulation ---------------------------------------------------------------------

    def run(self, operation: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        self.executed.append(operation)
        explain = _EXPLAIN.match(operation)
        sql = operation[explain.end() :] if explain else operation
        try:
            statement = sqlglot.parse_one(sql, read=self._dialect)
        except ParseError as error:
            raise _user_error("SYNTAX_ERROR", str(error).splitlines()[0][:200]) from error
        tables = self._referenced_tables(statement)
        for table in tables:
            if table not in self._tables:
                raise _user_error("TABLE_NOT_FOUND", f"Table '{table}' does not exist")
        try:
            qualified = qualify(statement, schema=self._schema, dialect=self._dialect)
        except OptimizeError as error:
            raise _user_error("COLUMN_NOT_FOUND", str(error)[:200]) from error
        for table in tables:
            if not self._policy.allows(self._user, table):
                raise _user_error(
                    "PERMISSION_DENIED", f"Access Denied: Cannot select from table {table}"
                )
        if explain:
            lines = ["Fragment 0 [SINGLE]", f"    Output[{', '.join(_output_names(qualified))}]"]
            for table in tables:
                lines.append(f"    TableScan[{table}]")
            return ["Query Plan"], [(line,) for line in lines]
        names = _output_names(qualified)
        limit = _limit_of(qualified)
        count = self._default_rows if limit is None else min(limit, self._default_rows)
        rows = [tuple(f"<{name}:{i + 1}>" for name in names) for i in range(count)]
        return names, rows

    def _referenced_tables(self, statement: exp.Expr) -> list[str]:
        cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
        names: list[str] = []
        for table in statement.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                continue
            if not table.db and not table.catalog and table.name.lower() in cte_names:
                continue
            parts = [p for p in (table.catalog, table.db, table.name) if p]
            name = ".".join(parts).lower()
            if len(parts) < 3:
                matches = [t for t in self._tables if t.endswith("." + name)]
                name = matches[0] if len(matches) == 1 else name
            if name not in names:
                names.append(name)
        return names


def _as_schema_dict(schema_map: SchemaMap) -> dict[str, Any]:
    return {
        catalog: {schema: dict(tables) for schema, tables in schemas.items()}
        for catalog, schemas in schema_map.items()
    }


def _output_names(statement: exp.Expr) -> list[str]:
    query = statement
    while isinstance(query, exp.Subquery):
        query = query.this
    if isinstance(query, exp.Query):
        names = [name for name in query.named_selects if name]
        return names or ["_col0"]
    return ["_col0"]


def _limit_of(statement: exp.Expr) -> int | None:
    limit = statement.args.get("limit")
    if isinstance(limit, exp.Limit) and isinstance(limit.expression, exp.Literal):
        try:
            return int(limit.expression.this)
        except ValueError:
            return None
    return None


def make_fixture_connection_factory(
    schema_map: SchemaMap, *, user: str, access: Mapping[str, Sequence[str]]
) -> ConnectionFactory:
    policy = FixtureAccessPolicy(access)

    def factory() -> ConnectionLike:
        return FixtureConnection(schema_map, user=user, policy=policy)

    return factory
