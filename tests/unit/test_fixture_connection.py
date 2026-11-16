from __future__ import annotations

import pytest
from trino.exceptions import TrinoUserError

from data_navigator.catalog.sqlite_repo import SqliteCatalogRepository
from data_navigator.config import Settings
from data_navigator.starburst.end_user_client import EndUserClient, build_end_user_client
from data_navigator.starburst.errors import access_decision
from data_navigator.starburst.fixture_connection import (
    FixtureAccessPolicy,
    FixtureConnection,
    make_fixture_connection_factory,
)
from tests.conftest import FIXTURE_ACCESS, fixture_settings, no_secrets

ACCOUNT_SQL = "SELECT customer_id, balance FROM cos.retail_ods.account WHERE status = 'OPEN'"


def connection(repo: SqliteCatalogRepository, user: str) -> FixtureConnection:
    return FixtureConnection(
        repo.schema_map(), user=user, policy=FixtureAccessPolicy(FIXTURE_ACCESS)
    )


def test_explain_and_select_are_simulated(enriched_repo: SqliteCatalogRepository) -> None:
    conn = connection(enriched_repo, "alice")
    columns, rows = conn.run("EXPLAIN " + ACCOUNT_SQL)
    assert columns == ["Query Plan"] and rows[0] == ("Fragment 0 [SINGLE]",)
    assert any("TableScan[cos.retail_ods.account]" in row[0] for row in rows)
    columns, rows = conn.run("EXPLAIN (TYPE VALIDATE) " + ACCOUNT_SQL)
    assert columns == ["Query Plan"]
    columns, rows = conn.run(ACCOUNT_SQL + " LIMIT 2")
    assert columns == ["customer_id", "balance"] and len(rows) == 2
    assert rows[0] == ("<customer_id:1>", "<balance:1>")
    columns, rows = conn.run("SELECT count(*) FROM cos.retail_ods.account")
    assert columns == ["_col_0"] and len(rows) == 3
    assert conn.executed and not conn.closed
    conn.close()
    assert conn.closed


def test_ctes_and_partial_names_resolve(enriched_repo: SqliteCatalogRepository) -> None:
    conn = connection(enriched_repo, "eve")
    sql = (
        "WITH latest AS (SELECT customer_id, max(score_date) AS d FROM cos.risk_dm.customer_risk_score "
        "GROUP BY customer_id) SELECT l.customer_id FROM latest l JOIN customer c ON l.customer_id = c.customer_id"
    )
    columns, _rows = conn.run(sql)
    assert columns == ["customer_id"]


@pytest.mark.parametrize(
    ("user", "sql", "error_name"),
    [
        ("bob", "SELECT customer_id FROM cos.risk_dm.customer_risk_score", "PERMISSION_DENIED"),
        ("alice", "SELECT x FROM cos.retail_ods.account", "COLUMN_NOT_FOUND"),
        ("alice", "SELECT 1 FROM cos.retail_ods.ghost", "TABLE_NOT_FOUND"),
        ("alice", "SELEC 1 FORM x", "SYNTAX_ERROR"),
        ("nobody", "SELECT customer_id FROM cos.retail_ods.account", "PERMISSION_DENIED"),
    ],
)
def test_errors_mirror_trino_codes(
    enriched_repo: SqliteCatalogRepository, user: str, sql: str, error_name: str
) -> None:
    conn = connection(enriched_repo, user)
    with pytest.raises(TrinoUserError) as info:
        conn.run(sql)
    assert info.value.error_name == error_name


def test_end_user_client_over_fixture_connection(enriched_repo: SqliteCatalogRepository) -> None:
    factory = make_fixture_connection_factory(
        enriched_repo.schema_map(), user="bob", access=FIXTURE_ACCESS
    )
    client = EndUserClient(factory, max_rows=5)
    assert client.explain(ACCOUNT_SQL).ok
    denied = client.explain("SELECT * FROM cos.risk_dm.daily_exposure LIMIT 0")
    assert denied.status == "permission_denied" and access_decision(denied.status) == "denied"
    semantic = client.explain("SELECT nope FROM cos.retail_ods.account")
    assert semantic.status == "semantic_error" and semantic.error_name == "COLUMN_NOT_FOUND"
    sample = client.sample(ACCOUNT_SQL, max_rows=50)
    assert sample.ok and sample.row_limit == 5 and sample.row_count == 3
    assert sample.executed_sql is not None and sample.executed_sql.endswith("LIMIT 5")
    rejected = client.sample("DELETE FROM cos.retail_ods.account")
    assert rejected.status == "rejected"


def test_build_end_user_client_uses_fixture_source(enriched_repo: SqliteCatalogRepository) -> None:
    settings = fixture_settings()
    client = build_end_user_client(
        settings, no_secrets(), user="carol", schema_map=enriched_repo.schema_map()
    )
    assert client.explain("SELECT customer_id FROM cos.risk_dm.daily_exposure").ok
    assert (
        client.explain("SELECT customer_id FROM cos.retail_ods.customer").status
        == "permission_denied"
    )
    empty = build_end_user_client(Settings(), no_secrets(), user="carol")
    assert empty.explain("SELECT 1 FROM cos.retail_ods.customer").status == "semantic_error"
