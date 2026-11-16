from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from data_navigator.config import Secrets, Settings
from data_navigator.starburst import builder_client as module
from data_navigator.starburst.builder_client import (
    BuilderClient,
    BuilderQueryError,
    build_builder_client,
    builder_allowed_tables,
)
from data_navigator.starburst.whitelist import BuilderPolicyError
from tests.fakes import FakeConnection, FakeFactory, trino_error


def test_policy_violation_never_opens_a_connection() -> None:
    factory = FakeFactory(FakeConnection())
    client = BuilderClient(factory)
    with pytest.raises(BuilderPolicyError):
        client.query("SELECT * FROM hive.ods.customer")
    with pytest.raises(BuilderPolicyError):
        client.query("DROP TABLE hive.ods.customer")
    assert factory.calls == 0


def test_whitelisted_query_returns_rows_and_columns() -> None:
    connection = FakeConnection(rows=[("a", 1), ("b", 2)], columns=["table_name", "n"])
    factory = FakeFactory(connection)
    client = BuilderClient(factory, allowed_tables=["hist.logs.query_history"])
    result = client.query("SELECT table_name, n FROM hive.information_schema.tables")
    assert result.columns == ["table_name", "n"]
    assert result.rows == [("a", 1), ("b", 2)]
    assert connection.executed == ["SELECT table_name, n FROM hive.information_schema.tables"]
    assert connection.closed and connection.cursors[0].closed
    assert client.allowed_tables == ("hist.logs.query_history",)


def test_server_errors_are_mapped() -> None:
    connection = FakeConnection(error=trino_error("PERMISSION_DENIED", message="denied"))
    client = BuilderClient(FakeFactory(connection))
    with pytest.raises(BuilderQueryError) as info:
        client.query("SELECT * FROM system.metadata.table_comments")
    assert info.value.mapped.status == "permission_denied"
    assert connection.closed


def test_check_does_not_execute() -> None:
    factory = FakeFactory(FakeConnection())
    client = BuilderClient(factory)
    client.check("SHOW CATALOGS")
    assert factory.calls == 0


def test_allowed_tables_follow_configuration() -> None:
    settings = Settings()
    assert builder_allowed_tables(settings) == ()
    settings.query_history.table = "hist.logs.query_history"
    assert builder_allowed_tables(settings) == ("hist.logs.query_history",)


def test_build_builder_client_wires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_connect(**kwargs: Any) -> FakeConnection:
        captured.update(kwargs)
        return FakeConnection()

    import trino.auth
    import trino.dbapi

    monkeypatch.setattr(trino.dbapi, "connect", fake_connect)
    monkeypatch.setattr(trino.auth, "BasicAuthentication", lambda user, password: ("basic", user))

    settings = Settings()
    settings.query_history.table = "hist.logs.query_history"
    secrets = Secrets(_env_file=None, builder_password=SecretStr("pw"))  # type: ignore[call-arg]
    client = build_builder_client(settings, secrets)
    client.query("SHOW CATALOGS")
    assert captured["user"] == "data_navigator_builder"
    assert captured["auth"] == ("basic", "data_navigator_builder")
    assert captured["source"] == "data-navigator-builder"
    assert client.allowed_tables == ("hist.logs.query_history",)
    assert module.BuilderClient is BuilderClient
