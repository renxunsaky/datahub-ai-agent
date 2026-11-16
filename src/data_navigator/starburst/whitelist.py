"""Statement whitelist enforced by the builder client before anything is sent.

Allowed, and nothing else:

* ``SHOW ...`` and ``DESCRIBE ...``;
* ``SELECT`` statements whose every table is ``<catalog>.information_schema.*``,
  ``system.metadata.*`` or one of the explicitly configured history tables.

Decisions are made on the parsed AST (statement type and referenced tables), not
on the raw text.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlglot import exp
from sqlglot.errors import ParseError

from data_navigator.sqlproc.readonly_guard import check_readonly_expression
from data_navigator.sqlproc.statements import (
    DEFAULT_DIALECT,
    StatementError,
    parse_single_statement,
)


class BuilderPolicyError(PermissionError):
    """The builder client refused to send a statement."""


def normalise_table_name(name: str, dialect: str = DEFAULT_DIALECT) -> str:
    """``catalog.schema.table`` in lower case, quotes removed."""
    try:
        table = exp.to_table(name.strip(), dialect=dialect)
    except ParseError:
        return name.strip().replace('"', "").lower()
    return ".".join(part.name.lower() for part in table.parts)


def check_builder_statement(
    sql: str,
    *,
    allowed_tables: Iterable[str] = (),
    dialect: str = DEFAULT_DIALECT,
) -> exp.Expr:
    """Validate *sql* against the builder policy and return its AST.

    Raises :class:`BuilderPolicyError` for anything outside the whitelist.
    """
    try:
        statement = parse_single_statement(sql, dialect)
    except StatementError as error:
        raise BuilderPolicyError(str(error)) from error

    if isinstance(statement, exp.Command):
        keyword = str(statement.this).upper()
        if keyword == "SHOW":
            return statement
        raise BuilderPolicyError(f"statement type {keyword} is not allowed for the builder")

    if isinstance(statement, exp.Describe):
        return statement

    if not isinstance(statement, exp.Select | exp.SetOperation):
        kind = type(statement).__name__.upper()
        raise BuilderPolicyError(f"statement type {kind} is not allowed for the builder")

    guard = check_readonly_expression(statement)
    if guard.rejected:
        raise BuilderPolicyError(guard.reason or "statement is not read-only")

    allowed = {normalise_table_name(table, dialect) for table in allowed_tables}
    cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    for table in statement.find_all(exp.Table):
        if not table.db and not table.catalog and table.name.lower() in cte_names:
            continue
        if not _is_allowed_table(table, allowed):
            raise BuilderPolicyError(
                f"table {table.sql(dialect=dialect)} is outside the builder whitelist"
            )
    return statement


def _is_allowed_table(table: exp.Table, allowed: set[str]) -> bool:
    catalog = table.catalog.lower()
    schema = table.db.lower()
    name = table.name.lower()
    if schema == "information_schema":
        return True
    if catalog == "system" and schema == "metadata":
        return True
    if catalog and schema:
        return f"{catalog}.{schema}.{name}" in allowed
    return False
