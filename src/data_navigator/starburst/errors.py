"""Deterministic mapping of Trino/Starburst errors.

The mapping is intentionally conservative: only an explicit ``PERMISSION_DENIED``
becomes ``denied``, only a successful statement becomes ``allowed``, and every
other failure (timeouts, connection problems, unexpected exceptions) is ``unknown``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Status = Literal[
    "ok",
    "rejected",
    "permission_denied",
    "syntax_error",
    "semantic_error",
    "timeout",
    "unknown",
]
AccessDecision = Literal["allowed", "denied", "unknown"]

# Error names that mean the statement itself is wrong and could be rewritten.
_SEMANTIC_ERROR_NAMES: frozenset[str] = frozenset(
    {
        "AMBIGUOUS_NAME",
        "CATALOG_NOT_FOUND",
        "COLUMN_NOT_FOUND",
        "COLUMN_TYPE_UNKNOWN",
        "DUPLICATE_COLUMN_NAME",
        "DUPLICATE_NAMED_QUERY",
        "EXPRESSION_NOT_AGGREGATE",
        "EXPRESSION_NOT_CONSTANT",
        "EXPRESSION_NOT_IN_DISTINCT",
        "EXPRESSION_NOT_SCALAR",
        "FUNCTION_NOT_FOUND",
        "INVALID_ARGUMENTS",
        "INVALID_CAST_ARGUMENT",
        "INVALID_COLUMN_REFERENCE",
        "INVALID_FUNCTION_ARGUMENT",
        "INVALID_LIMIT_CLAUSE",
        "INVALID_LITERAL",
        "INVALID_ORDER_BY",
        "INVALID_PARAMETER_USAGE",
        "INVALID_RECURSIVE_REFERENCE",
        "INVALID_WINDOW_FRAME",
        "MISMATCHED_COLUMN_ALIASES",
        "MISSING_CATALOG_NAME",
        "MISSING_COLUMN_ALIASES",
        "MISSING_COLUMN_NAME",
        "MISSING_GROUP_BY",
        "MISSING_ORDER_BY",
        "MISSING_SCHEMA_NAME",
        "NESTED_AGGREGATION",
        "NESTED_WINDOW",
        "NOT_SUPPORTED",
        "NULL_TREATMENT_NOT_ALLOWED",
        "OPERATOR_NOT_FOUND",
        "SCHEMA_NOT_FOUND",
        "TABLE_NOT_FOUND",
        "TOO_MANY_ARGUMENTS",
        "TOO_MANY_GROUPING_SETS",
        "TYPE_MISMATCH",
        "TYPE_NOT_FOUND",
        "VIEW_IS_RECURSIVE",
        "VIEW_IS_STALE",
    }
)

_TIMEOUT_ERROR_NAMES: frozenset[str] = frozenset(
    {
        "EXCEEDED_TIME_LIMIT",
        "EXCEEDED_CPU_LIMIT",
        "EXCEEDED_SCAN_LIMIT",
        "EXCEEDED_MEMORY_LIMIT",
        "EXCEEDED_LOCAL_MEMORY_LIMIT",
        "EXCEEDED_GLOBAL_MEMORY_LIMIT",
        "EXCEEDED_SPILL_LIMIT",
        "EXCEEDED_WRITE_LIMIT",
        "QUERY_QUEUE_FULL",
        "CLUSTER_OUT_OF_MEMORY",
    }
)


@dataclass(frozen=True)
class MappedError:
    """A Trino failure reduced to a status, a short message and the error name."""

    status: Status
    message: str
    error_name: str | None = None


def map_exception(error: BaseException) -> MappedError:
    """Classify *error* without ever producing ``ok``."""
    error_name = getattr(error, "error_name", None)
    error_type = getattr(error, "error_type", None)
    message = _short_message(error)

    if isinstance(error_name, str):
        name = error_name.upper()
        if name == "PERMISSION_DENIED":
            return MappedError("permission_denied", message, name)
        if name in _TIMEOUT_ERROR_NAMES:
            return MappedError("timeout", message, name)
        if name == "SYNTAX_ERROR":
            return MappedError("syntax_error", message, name)
        if error_type == "USER_ERROR" and name in _SEMANTIC_ERROR_NAMES:
            return MappedError("semantic_error", message, name)
        return MappedError("unknown", message, name)

    if isinstance(error, TimeoutError):
        return MappedError("timeout", message)
    return MappedError("unknown", message)


def access_decision(status: Status) -> AccessDecision:
    """Reduce a statement outcome to an access decision; never ``allowed`` on failure."""
    if status == "ok":
        return "allowed"
    if status == "permission_denied":
        return "denied"
    return "unknown"


def _short_message(error: BaseException) -> str:
    message = getattr(error, "message", None)
    text = message if isinstance(message, str) and message else str(error)
    text = text.strip() or error.__class__.__name__
    # Keep messages short; they are shown to the model and written to the audit log.
    return text[:500]
