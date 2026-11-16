from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from data_navigator.config import QueryHistoryColumns, QueryHistorySettings
from data_navigator.sources.query_history import (
    JsonlQueryHistorySource,
    StarburstQueryHistorySource,
    parse_timestamp,
)
from data_navigator.starburst.builder_client import BuilderClient
from data_navigator.starburst.whitelist import BuilderPolicyError, check_builder_statement
from tests.fakes import RoutingConnection

FIXTURE = Path("tests/fixtures/query_history/queries.jsonl")


def test_jsonl_source_skips_failed_queries_and_parses_fields() -> None:
    records = list(
        JsonlQueryHistorySource(FIXTURE, QueryHistorySettings()).fetch(since=None, until=None)
    )
    assert len(records) == 25  # 26 lines, one FAILED
    assert all(r.state == "FINISHED" for r in records)
    first = records[0]
    assert first.user == "alice" and first.catalog == "cos" and first.schema == "retail_ods"
    assert first.started_at == datetime(2026, 10, 1, 8, 15, tzinfo=UTC)
    assert first.ended_at == first.started_at


def test_jsonl_source_honours_window() -> None:
    settings = QueryHistorySettings()
    since = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)
    until = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
    records = list(JsonlQueryHistorySource(FIXTURE, settings).fetch(since=since, until=until))
    assert {r.query_id for r in records} == {
        "q_bob_3",
        "q_eve_3",
        "q_alice_unnest",
        "q_alice_window",
    }
    # boundaries: > since, <= until
    exact = list(JsonlQueryHistorySource(FIXTURE, settings).fetch(since=until, until=until))
    assert exact == []


def test_jsonl_source_with_custom_column_names(tmp_path: Path) -> None:
    path = tmp_path / "h.jsonl"
    path.write_text(
        '{"id": "1", "sql": "SELECT 1", "who": "x", "status": "finished", "begin": "2026-01-01 00:00:00"}\n'
        "\n"
        '{"id": "2", "sql": "SELECT 2", "who": "x", "status": "FAILED", "begin": "2026-01-01 00:00:00"}\n'
        '{"id": "3", "sql": "SELECT 3", "who": "x", "status": "FINISHED"}\n'
    )
    settings = QueryHistorySettings(
        columns=QueryHistoryColumns(
            query_id="id",
            query_text="sql",
            user="who",
            state="status",
            started_at="begin",
            ended_at=None,
            catalog=None,
            schema=None,
        )
    )
    records = list(JsonlQueryHistorySource(path, settings).fetch(since=None, until=None))
    assert [r.query_id for r in records] == ["1"]
    assert records[0].ended_at is None and records[0].catalog is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("2026-01-02T03:04:05Z", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
        ("2026-01-02 03:04:05", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
        ("2026-01-02T05:04:05+02:00", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
        (datetime(2026, 1, 2, 3, 4, 5), datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
    ],
)
def test_parse_timestamp(value: object, expected: datetime | None) -> None:
    assert parse_timestamp(value) == expected


def test_starburst_source_requires_table() -> None:
    client = BuilderClient(lambda: RoutingConnection(lambda sql: ([], [])))
    with pytest.raises(ValueError, match=r"query_history\.table"):
        StarburstQueryHistorySource(client, QueryHistorySettings(source="starburst"))


def test_starburst_source_builds_whitelisted_query_and_maps_rows() -> None:
    settings = QueryHistorySettings(source="starburst", table="hist.logs.query_history")
    since = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    until = datetime(2026, 10, 2, 12, 30, 15, 123000, tzinfo=UTC)

    def route(sql: str) -> tuple[list[str], list[tuple[object, ...]]]:
        check_builder_statement(sql, allowed_tables=["hist.logs.query_history"])
        return (
            [
                "query_id",
                "query_text",
                "user",
                "state",
                "started_at",
                "ended_at",
                "catalog",
                "schema",
            ],
            [
                (
                    "q1",
                    "SELECT 1",
                    "alice",
                    "FINISHED",
                    "2026-10-01T10:00:00Z",
                    "2026-10-01T10:00:05Z",
                    "cos",
                    "retail_ods",
                ),
                ("q2", "SELECT 2", None, "FINISHED", None, None, None, None),
            ],
        )

    connection = RoutingConnection(route)
    client = BuilderClient(lambda: connection, allowed_tables=["hist.logs.query_history"])
    source = StarburstQueryHistorySource(client, settings)
    sql = source.build_query(since=since, until=until)
    assert '"hist"."logs"."query_history"' in sql
    assert "'2026-10-01 00:00:00.000'" in sql and "'2026-10-02 12:30:15.123'" in sql
    assert '"end" >' in sql and '"end" <=' in sql  # window on the ended_at column
    assert "'FINISHED'" in sql

    records = list(source.fetch(since=since, until=until))
    assert [r.query_id for r in records] == ["q1"]  # q2 has no start time
    assert records[0].user == "alice" and records[0].schema == "retail_ods"
    assert records[0].ended_at == datetime(2026, 10, 1, 10, 0, 5, tzinfo=UTC)
    assert connection.executed == [sql]


def test_starburst_source_query_is_refused_when_table_not_allowed() -> None:
    settings = QueryHistorySettings(source="starburst", table="hist.logs.query_history")
    client = BuilderClient(lambda: RoutingConnection(lambda sql: ([], [])))  # no allowed tables
    source = StarburstQueryHistorySource(client, settings)
    with pytest.raises(BuilderPolicyError):
        list(source.fetch(since=None, until=None))


def test_window_falls_back_to_started_at_without_ended_at() -> None:
    settings = QueryHistorySettings(
        source="starburst",
        table="hist.logs.q",
        columns=QueryHistoryColumns(ended_at=None, started_at="created"),
    )
    client = BuilderClient(
        lambda: RoutingConnection(lambda sql: ([], [])), allowed_tables=["hist.logs.q"]
    )
    sql = StarburstQueryHistorySource(client, settings).build_query(
        since=datetime(2026, 1, 1, tzinfo=UTC), until=None
    )
    assert '"created" >' in sql and "<=" not in sql
