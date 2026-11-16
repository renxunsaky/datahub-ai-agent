"""Helpers shared by the guard, the whitelist and the normaliser."""

from __future__ import annotations

import logging

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

DEFAULT_DIALECT = "trino"


class StatementError(ValueError):
    """Raised when *sql* is not exactly one parseable statement.

    ``code`` is ``"unparseable"``, ``"empty"`` or ``"multiple"``.
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def quiet_sqlglot() -> None:
    """Silence sqlglot's "falling back to Command" warnings.

    Those warnings are expected for ``SHOW``/``CALL``/... statements, which the
    guard and the whitelist classify explicitly.
    """
    logging.getLogger("sqlglot").setLevel(logging.ERROR)


def parse_single_statement(sql: str, dialect: str = DEFAULT_DIALECT) -> exp.Expr:
    """Parse *sql* and return its AST, requiring exactly one non-empty statement.

    Comments are stripped by the parser, so a statement hidden in a comment does
    not count; a second real statement after ``;`` does.
    """
    try:
        parsed = sqlglot.parse(sql, read=dialect)
    except ParseError as error:
        msg = f"unparseable SQL ({error.__class__.__name__})"
        raise StatementError(msg, "unparseable") from error
    # ``None`` and ``Semicolon`` are empty statements (``;;`` or a trailing comment).
    statements: list[exp.Expr] = [
        statement
        for statement in parsed
        if statement is not None and not isinstance(statement, exp.Semicolon)
    ]
    if len(statements) != 1:
        msg = f"expected exactly one statement, found {len(statements)}"
        raise StatementError(msg, "empty" if not statements else "multiple")
    return statements[0]


def is_table_function(table: exp.Table) -> bool:
    """``TABLE(...)`` / ``FROM f(...)``: table-valued functions may proxy writes."""
    return not isinstance(table.this, exp.Identifier)
