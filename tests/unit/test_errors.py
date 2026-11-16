from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from trino.exceptions import TrinoConnectionError, TrinoExternalError, TrinoInternalError

from data_navigator.starburst.errors import (
    AccessDecision,
    Status,
    access_decision,
    map_exception,
)
from tests.fakes import trino_error

ALL_STATUSES: list[Status] = [
    "ok",
    "rejected",
    "permission_denied",
    "syntax_error",
    "semantic_error",
    "timeout",
    "unknown",
]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (trino_error("PERMISSION_DENIED", message="Access Denied"), "permission_denied"),
        (trino_error("permission_denied"), "permission_denied"),
        (trino_error("SYNTAX_ERROR"), "syntax_error"),
        (trino_error("COLUMN_NOT_FOUND"), "semantic_error"),
        (trino_error("TABLE_NOT_FOUND"), "semantic_error"),
        (trino_error("TYPE_MISMATCH"), "semantic_error"),
        (trino_error("EXCEEDED_TIME_LIMIT"), "timeout"),
        (trino_error("EXCEEDED_SCAN_LIMIT"), "timeout"),
        (trino_error("SOMETHING_NEW"), "unknown"),
        (trino_error("COLUMN_NOT_FOUND", error_type="INTERNAL_ERROR"), "unknown"),
        (TrinoInternalError({"errorName": "GENERIC_INTERNAL_ERROR"}, "q"), "unknown"),
        (TrinoExternalError({"errorName": "HIVE_METASTORE_ERROR"}, "q"), "unknown"),
        (TrinoConnectionError("connection refused"), "unknown"),
        (TimeoutError("timed out"), "timeout"),
        (ConnectionError("reset"), "unknown"),
        (RuntimeError("anything"), "unknown"),
        (ValueError(""), "unknown"),
    ],
)
def test_map_exception(error: BaseException, expected: Status) -> None:
    mapped = map_exception(error)
    assert mapped.status == expected
    assert mapped.status != "ok"
    assert mapped.message


def test_message_is_truncated_and_keeps_error_name() -> None:
    mapped = map_exception(trino_error("TYPE_MISMATCH", message="x" * 2000))
    assert len(mapped.message) == 500
    assert mapped.error_name == "TYPE_MISMATCH"


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_access_decision_only_allows_ok(status: Status) -> None:
    decision: AccessDecision = access_decision(status)
    if status == "ok":
        assert decision == "allowed"
    elif status == "permission_denied":
        assert decision == "denied"
    else:
        assert decision == "unknown"


@given(
    error_name=st.text(min_size=0, max_size=40),
    error_type=st.sampled_from(
        ["USER_ERROR", "INTERNAL_ERROR", "EXTERNAL", "INSUFFICIENT_RESOURCES"]
    ),
)
def test_any_exception_never_maps_to_ok(error_name: str, error_type: str) -> None:
    mapped = map_exception(trino_error(error_name, error_type=error_type))
    assert mapped.status != "ok"
    assert access_decision(mapped.status) != "allowed"
