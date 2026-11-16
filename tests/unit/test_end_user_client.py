from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr
from sqlglot import parse_one

from data_navigator.config import Secrets, Settings, StarburstConnectionSettings
from data_navigator.sqlproc.readonly_guard import readonly_guard
from data_navigator.starburst.connection import Credentials, make_connection_factory
from data_navigator.starburst.end_user_client import (
    EndUserClient,
    build_end_user_client,
    sample_session_properties,
    sql_digest,
)
from data_navigator.starburst.errors import access_decision
from tests.fakes import FakeConnection, FakeFactory, trino_error

BUSINESS_SQL = "SELECT customer_id, balance FROM cos.retail_ods.account WHERE status = 'open'"


def make_client(
    connection: FakeConnection, max_rows: int = 10
) -> tuple[EndUserClient, FakeFactory]:
    factory = FakeFactory(connection)
    return EndUserClient(factory, max_rows=max_rows), factory


# -- explain ----------------------------------------------------------------------


def test_explain_runs_explain_and_returns_plan() -> None:
    connection = FakeConnection(rows=[("Fragment 0",), ("  Output",)], columns=["Query Plan"])
    client, factory = make_client(connection)
    result = client.explain(BUSINESS_SQL)
    assert result.ok and result.status == "ok"
    assert result.plan == "Fragment 0\n  Output"
    assert connection.executed[0].startswith("EXPLAIN SELECT")
    assert factory.calls == 1
    assert connection.closed


def test_explain_validate_only() -> None:
    connection = FakeConnection(rows=[(True,)], columns=["Valid"])
    client, _ = make_client(connection)
    assert client.explain(BUSINESS_SQL, validate_only=True).ok
    assert connection.executed[0].startswith("EXPLAIN (TYPE VALIDATE) SELECT")


@pytest.mark.parametrize(
    "sql",
    ["DROP TABLE cos.retail_ods.account", "SELECT 1; DELETE FROM t", "CALL x()", ""],
)
def test_explain_rejects_non_select_without_connecting(sql: str) -> None:
    client, factory = make_client(FakeConnection())
    result = client.explain(sql)
    assert result.status == "rejected"
    assert result.message
    assert factory.calls == 0
    assert access_decision(result.status) == "unknown"


@pytest.mark.parametrize(
    ("error", "status", "decision"),
    [
        (trino_error("PERMISSION_DENIED"), "permission_denied", "denied"),
        (trino_error("SYNTAX_ERROR"), "syntax_error", "unknown"),
        (trino_error("COLUMN_NOT_FOUND"), "semantic_error", "unknown"),
        (trino_error("EXCEEDED_TIME_LIMIT"), "timeout", "unknown"),
        (ConnectionError("down"), "unknown", "unknown"),
        (RuntimeError("weird"), "unknown", "unknown"),
    ],
)
def test_explain_maps_errors_and_never_allows_on_failure(
    error: BaseException, status: str, decision: str
) -> None:
    client, _ = make_client(FakeConnection(error=error))
    result = client.explain(BUSINESS_SQL)
    assert result.status == status
    assert not result.ok
    assert access_decision(result.status) == decision


# -- sample -----------------------------------------------------------------------


def _executed_limit(sql: str) -> int:
    node = parse_one(sql, read="trino").args["limit"]
    return int(node.expression.this)


@pytest.mark.parametrize(
    ("sql", "requested", "expected"),
    [
        (BUSINESS_SQL, None, 10),
        (BUSINESS_SQL + " LIMIT 1000", None, 10),
        (BUSINESS_SQL + " LIMIT 3", None, 3),
        (BUSINESS_SQL, 50, 10),
        (BUSINESS_SQL, 4, 4),
        (BUSINESS_SQL + " LIMIT 2", 4, 2),
        ("SELECT a FROM t UNION ALL SELECT a FROM u LIMIT 500", None, 10),
        ("WITH x AS (SELECT a FROM t LIMIT 500) SELECT * FROM x", None, 10),
        ("SELECT a FROM t FETCH FIRST 500 ROWS ONLY", None, 10),
    ],
)
def test_sample_row_limit_cannot_be_exceeded(
    sql: str, requested: int | None, expected: int
) -> None:
    connection = FakeConnection(rows=[(i,) for i in range(100)], columns=["c"])
    client, _ = make_client(connection)
    result = client.sample(sql, requested)
    assert result.ok
    assert result.row_limit == expected
    assert result.executed_sql == connection.executed[0]
    assert _executed_limit(connection.executed[0]) == expected
    assert readonly_guard(connection.executed[0]).allowed
    # Even a misbehaving server cannot hand back more rows than the limit.
    assert result.row_count == expected
    assert result.columns == ["c"]


def test_sample_rejects_requests_below_one() -> None:
    client, factory = make_client(FakeConnection())
    with pytest.raises(ValueError, match="at least 1"):
        client.sample(BUSINESS_SQL, 0)
    assert factory.calls == 0
    with pytest.raises(ValueError, match="at least 1"):
        EndUserClient(factory, max_rows=0)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM cos.retail_ods.account",
        "SELECT 1; DROP TABLE t",
        "SELECT * INTO x FROM t",
        "SELECT * FROM TABLE(system.query(query => 'DELETE FROM t'))",
        "INSERT INTO t SELECT 1",
    ],
)
def test_sample_rejects_non_select_without_connecting(sql: str) -> None:
    client, factory = make_client(FakeConnection())
    result = client.sample(sql)
    assert result.status == "rejected"
    assert result.rows == [] and result.row_count == 0
    assert factory.calls == 0


def test_sample_refuses_to_run_if_limit_was_not_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    import data_navigator.starburst.end_user_client as module

    monkeypatch.setattr(module, "apply_row_limit", lambda statement, max_rows: statement)
    client, factory = make_client(FakeConnection())
    with pytest.raises(RuntimeError, match="row limit was not applied"):
        client.sample(BUSINESS_SQL)
    assert factory.calls == 0


def test_sample_maps_server_errors() -> None:
    client, _ = make_client(FakeConnection(error=trino_error("PERMISSION_DENIED")))
    result = client.sample(BUSINESS_SQL)
    assert result.status == "permission_denied"
    assert result.rows == []
    assert result.executed_sql is not None and "LIMIT 10" in result.executed_sql


def test_sample_result_never_logs_rows(caplog: pytest.LogCaptureFixture) -> None:
    connection = FakeConnection(rows=[("SECRET-VALUE-42",)], columns=["c"])
    client, _ = make_client(connection)
    with caplog.at_level("DEBUG"):
        result = client.sample("SELECT c FROM t WHERE k = 'FILTER-CONST'")
    assert result.rows == [("SECRET-VALUE-42",)]
    assert "SECRET-VALUE-42" not in caplog.text
    assert "FILTER-CONST" not in caplog.text


def test_sql_digest_is_stable_and_short() -> None:
    assert sql_digest("SELECT 1") == sql_digest("  SELECT 1 \n")
    assert len(sql_digest("SELECT 1")) == 16


# -- wiring -----------------------------------------------------------------------


def test_session_properties_include_time_limit() -> None:
    settings = Settings()
    props = sample_session_properties(settings.samples)
    assert props["query_max_execution_time"] == "30s"
    assert props["query_max_scan_physical_bytes"] == "1GB"


def test_build_end_user_client_requires_identity() -> None:
    with pytest.raises(RuntimeError, match="DN_END_USER_USER"):
        build_end_user_client(Settings(), Secrets(_env_file=None))  # type: ignore[call-arg]


def test_build_end_user_client_uses_harness_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_connect(**kwargs: Any) -> FakeConnection:
        captured.update(kwargs)
        return FakeConnection(rows=[("plan",)], columns=["Query Plan"])

    import trino.auth
    import trino.dbapi

    monkeypatch.setattr(trino.dbapi, "connect", fake_connect)
    monkeypatch.setattr(trino.auth, "BasicAuthentication", lambda user, password: ("basic", user))
    secrets = Secrets(  # type: ignore[call-arg]
        _env_file=None, end_user_user="alice", end_user_password=SecretStr("pw")
    )
    client = build_end_user_client(Settings(), secrets)
    assert client.max_rows == 10
    assert client.explain("SELECT 1").ok
    assert captured["user"] == "alice"
    assert captured["source"] == "data-navigator-end-user"
    assert captured["session_properties"]["query_max_execution_time"] == "30s"


def test_connection_factory_auth_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_connect(**kwargs: Any) -> FakeConnection:
        captured.update(kwargs)
        return FakeConnection()

    import trino.auth
    import trino.dbapi

    monkeypatch.setattr(trino.dbapi, "connect", fake_connect)
    monkeypatch.setattr(trino.auth, "JWTAuthentication", lambda token: ("jwt", token))

    jwt_settings = StarburstConnectionSettings(auth="jwt")
    factory = make_connection_factory(
        jwt_settings, Credentials(user="bob", jwt=SecretStr("tok")), source_suffix="x"
    )
    factory()
    assert captured["auth"] == ("jwt", "tok")

    none_settings = StarburstConnectionSettings(auth="none", http_scheme="http")
    make_connection_factory(none_settings, Credentials(user="bob"), source_suffix="x")()
    assert captured["auth"] is None

    with pytest.raises(RuntimeError, match="JWT missing"):
        make_connection_factory(jwt_settings, Credentials(user="bob"), source_suffix="x")()
    with pytest.raises(RuntimeError, match="password missing"):
        make_connection_factory(
            StarburstConnectionSettings(auth="basic"), Credentials(user="bob"), source_suffix="x"
        )()
