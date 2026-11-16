"""Constant removal and canonical formatting.

``normalize`` parses a statement (Trino first, Spark as a fallback) and returns
a canonical text in which every constant — strings, numbers, dates, timestamps,
intervals, hex/byte strings, ``IN`` lists, ``VALUES`` rows — is replaced by ``?``.
Comments are dropped and unquoted identifiers are lower-cased, so the same
query pattern with different constants or formatting yields identical text.

Positional ``GROUP BY 1`` / ``ORDER BY 2`` references are expanded to the
corresponding select expressions first, so no raw number survives and the
parser still knows which columns are grouped.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from data_navigator.sqlproc.statements import StatementError, parse_single_statement

DEFAULT_DIALECTS: tuple[str, ...] = ("trino", "spark")

_LITERAL_TYPES: tuple[type[exp.Expr], ...] = tuple(
    getattr(exp, name)
    for name in (
        "Literal",
        "HexString",
        "ByteString",
        "BitString",
        "UnicodeString",
        "RawString",
        "JSONPath",
    )
    if hasattr(exp, name)
)
_PRECISION_TYPES = frozenset(
    {
        exp.DataType.Type.TIMESTAMP,
        exp.DataType.Type.TIMESTAMPTZ,
        exp.DataType.Type.TIME,
        exp.DataType.Type.TIMETZ,
    }
)


class NormalizationError(ValueError):
    """The statement could not be parsed (or is not a query/DML/DDL statement)."""

    def __init__(self, error_type: str, dialects_tried: Sequence[str], sql_hash: str) -> None:
        super().__init__(f"{error_type} (dialects tried: {', '.join(dialects_tried)})")
        self.error_type = error_type
        self.dialects_tried = tuple(dialects_tried)
        self.sql_hash = sql_hash


class UnsupportedStatementError(NormalizationError):
    """Parsed fine, but it is not a SELECT/INSERT/CREATE/MERGE/DELETE/UPDATE."""


@dataclass(frozen=True)
class NormalizedSQL:
    text: str
    dialect: str
    statement_type: str
    expression: exp.Expr = field(compare=False, repr=False)


def raw_sql_hash(sql: str) -> str:
    """SHA-256 of the raw statement; the only thing kept for unparseable SQL."""
    return hashlib.sha256(sql.strip().encode("utf-8")).hexdigest()


def statement_type(expression: exp.Expr) -> str:
    if isinstance(expression, exp.Query):
        return "select"
    if isinstance(expression, exp.Insert):
        return "insert"
    if isinstance(expression, exp.Create):
        kind = str(expression.args.get("kind") or "").upper()
        if expression.expression is not None:
            return "ctas" if kind == "TABLE" else f"create_{kind.lower() or 'object'}_as"
        return "create"
    if isinstance(expression, exp.Merge):
        return "merge"
    if isinstance(expression, exp.Delete):
        return "delete"
    if isinstance(expression, exp.Update):
        return "update"
    return type(expression).__name__.lower()


_SUPPORTED: tuple[type[exp.Expr], ...] = (
    exp.Query,
    exp.Insert,
    exp.Create,
    exp.Merge,
    exp.Delete,
    exp.Update,
)


def normalize(
    sql: str,
    *,
    dialects: Sequence[str] = DEFAULT_DIALECTS,
    placeholder: str | None = None,
) -> NormalizedSQL:
    """Parse *sql* with the first dialect that accepts it and strip all constants.

    *placeholder* is the identifier substituted for template expressions; column
    references to it are treated as constants too.
    """
    sql_hash = raw_sql_hash(sql)
    last_error = "EmptyStatement"
    for dialect in dialects:
        try:
            expression = parse_single_statement(sql, dialect)
        except StatementError as error:
            cause = error.__cause__
            if error.code == "empty":
                last_error = "EmptyStatement"
            elif error.code == "multiple":
                last_error = "MultipleStatements"
            else:
                last_error = type(cause).__name__ if cause is not None else "StatementError"
            continue
        if not isinstance(expression, _SUPPORTED):
            kind = type(expression).__name__
            raise UnsupportedStatementError(f"Unsupported:{kind}", dialects, sql_hash)
        return _normalize_expression(expression, dialect, placeholder)
    raise NormalizationError(last_error, dialects, sql_hash)


def _normalize_expression(
    expression: exp.Expr, dialect: str, placeholder: str | None
) -> NormalizedSQL:
    tree = expression.copy()
    for select in list(tree.find_all(exp.Select)):
        _expand_positional_references(select)
    tree = tree.transform(_replace_constant)
    for cast in list(tree.find_all(exp.Cast)):
        _drop_inferred_precision(cast)
    if placeholder:
        wanted = placeholder.lower()
        tree = tree.transform(
            lambda node: (
                exp.Placeholder()
                if isinstance(node, exp.Column) and node.name.lower() == wanted
                else node
            )
        )
    for in_node in list(tree.find_all(exp.In)):
        _collapse_placeholder_list(in_node)
    for values in list(tree.find_all(exp.Values)):
        _collapse_values(values)
    tree = normalize_identifiers(tree, dialect=dialect)
    text = tree.sql(dialect=dialect, comments=False)
    return NormalizedSQL(
        text=text, dialect=dialect, statement_type=statement_type(tree), expression=tree
    )


def _replace_constant(node: exp.Expr) -> exp.Expr:
    if not isinstance(node, _LITERAL_TYPES):
        return node
    if node.find_ancestor(exp.DataType) is not None:  # e.g. DECIMAL(18, 2)
        return node
    return exp.Placeholder()


def _drop_inferred_precision(cast: exp.Cast) -> None:
    """``TIMESTAMP '2024-01-01 00:00:00.123'`` parses as ``CAST(? AS TIMESTAMP(3))``; the
    precision comes from the constant, so it is dropped to keep patterns stable."""
    data_type = cast.to
    if (
        isinstance(cast.this, exp.Placeholder)
        and isinstance(data_type, exp.DataType)
        and data_type.this in _PRECISION_TYPES
        and data_type.expressions
    ):
        data_type.set("expressions", [])


def _collapse_placeholder_list(node: exp.In) -> None:
    items = node.expressions
    if items and all(isinstance(item, exp.Placeholder) for item in items):
        node.set("expressions", [exp.Placeholder()])


def _collapse_values(node: exp.Values) -> None:
    rows = node.expressions
    if len(rows) > 1 and all(
        isinstance(row, exp.Tuple)
        and all(isinstance(item, exp.Placeholder) for item in row.expressions)
        for row in rows
    ):
        node.set("expressions", [rows[0]])


def _expand_positional_references(select: exp.Select) -> None:
    projections = select.expressions
    if any(isinstance(p, exp.Star) for p in projections):
        return
    group = select.args.get("group")
    if isinstance(group, exp.Group):
        group.set(
            "expressions",
            [_positional_target(item, projections) for item in group.expressions],
        )
    order = select.args.get("order")
    if isinstance(order, exp.Order):
        for ordered in order.expressions:
            if isinstance(ordered, exp.Ordered):
                ordered.set("this", _positional_target(ordered.this, projections))


def _positional_target(item: exp.Expr, projections: Sequence[exp.Expr]) -> exp.Expr:
    if isinstance(item, exp.Literal) and not item.is_string and item.this.isdigit():
        index = int(item.this)
        if 1 <= index <= len(projections):
            return projections[index - 1].unalias().copy()
    return item
