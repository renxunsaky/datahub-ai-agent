from __future__ import annotations

from typing import Any

import pytest

from data_navigator.agent.tools import ALL_TOOL_SPECS, AgentTools, quote_table, tables_in
from data_navigator.catalog.sqlite_repo import SqliteCatalogRepository
from data_navigator.llm.replay import ReplayLLM
from data_navigator.search.factory import build_search
from data_navigator.starburst.end_user_client import (
    EndUserClient,
    SampleResult,
    build_end_user_client,
)
from tests.conftest import fixture_settings, no_secrets

ACCOUNT = "cos.retail_ods.account"
CUSTOMER = "cos.retail_ods.customer"
RISK = "cos.risk_dm.customer_risk_score"
GOOD_SQL = (
    "SELECT c.segment_code, SUM(a.balance) AS total_balance FROM cos.retail_ods.account a "
    "JOIN cos.retail_ods.customer c ON a.customer_id = c.customer_id GROUP BY c.segment_code"
)


def make_tools(repo: SqliteCatalogRepository, user: str = "alice") -> AgentTools:
    settings = fixture_settings()
    client = build_end_user_client(settings, no_secrets(), user=user, schema_map=repo.schema_map())
    return AgentTools(
        repository=repo,
        search=build_search(settings, repo, ReplayLLM()),
        client=client,
        settings=settings.agent,
        samples=settings.samples,
    )


def test_tool_specs_are_openai_functions() -> None:
    names = [spec["function"]["name"] for spec in ALL_TOOL_SPECS]
    assert names == [
        "search_tables",
        "get_table_card",
        "get_join_path",
        "similar_queries",
        "validate_sql",
        "check_access",
        "run_sample",
        "final_answer",
    ]
    for spec in ALL_TOOL_SPECS:
        assert spec["type"] == "function"
        assert "user" not in spec["function"]["parameters"]["properties"]


def test_search_tables_returns_public_summaries(enriched_repo: SqliteCatalogRepository) -> None:
    tools = make_tools(enriched_repo)
    result = tools.search_tables("customer segment balance account")
    names = [t["table"] for t in result["tables"]]
    assert ACCOUNT in names and CUSTOMER in names and len(names) <= 8
    account = next(t for t in result["tables"] if t["table"] == ACCOUNT)
    assert account["owner_team"] == "retail" and account["purpose_source"] == "card (ai_inferred)"
    assert "columns" not in account
    assert tools.search_tables("x", limit=1)["count"] <= 1
    assert tools.search_tables("   ")["error"]
    assert tools.search_tables("customer", limit="many")["count"] >= 1  # type: ignore[arg-type]
    assert tables_in([result])[:2] == names[:2]


def test_get_table_card_respects_access(enriched_repo: SqliteCatalogRepository) -> None:
    alice = make_tools(enriched_repo, "alice")
    card = alice.get_table_card('"cos"."retail_ods"."account"')
    assert card["access"] == "allowed" and card["table"] == ACCOUNT
    assert {c["name"] for c in card["columns"]} >= {"account_id", "balance", "dt"}
    assert any(k["name"] == "balance" for k in card["key_columns"])
    assert card["join_edges"][0]["left"].startswith("cos.retail_ods.")

    bob = make_tools(enriched_repo, "bob")
    hidden = bob.get_table_card(RISK)
    assert (
        hidden["access"] == "denied" and "columns" not in hidden and "no access" in hidden["note"]
    )
    assert hidden["purpose"]  # public one-liner still visible
    assert "error" in alice.get_table_card("cos.nope.missing")


def test_get_join_path(enriched_repo: SqliteCatalogRepository) -> None:
    tools = make_tools(enriched_repo)
    path = tools.get_join_path([ACCOUNT, CUSTOMER])
    assert path["edges"] and path["missing"] == []
    assert path["edges"][0]["left"] == f"{ACCOUNT}.customer_id"
    assert path["edges"][0]["right"] == f"{CUSTOMER}.customer_id"
    indirect = tools.get_join_path([ACCOUNT, "cos.ref.branch"])
    assert indirect["edges"] and len(indirect["edges"]) >= 2  # account -> customer -> branch
    none = tools.get_join_path([ACCOUNT, "cos.retail_dm.v_active_customer"])
    assert (
        none["missing"] == [[ACCOUNT, "cos.retail_dm.v_active_customer"]]
        and "do not guess" in none["note"]
    )
    assert tools.get_join_path([ACCOUNT])["note"] == "one table: no join needed"
    assert "unknown tables" in tools.get_join_path([ACCOUNT, "cos.x.y"])["error"]
    assert "error" in tools.get_join_path([])
    assert "error" in tools.get_join_path("cos.retail_ods.account")  # type: ignore[arg-type]


def test_similar_queries_filters_by_access(enriched_repo: SqliteCatalogRepository) -> None:
    alice = make_tools(enriched_repo, "alice")
    result = alice.similar_queries("customer segment balance account")
    assert result["count"] >= 1
    assert all("?" in p["sql"] or "dn_tpl_var" in p["sql"] for p in result["patterns"])
    assert all("'OPEN'" not in p["sql"] for p in result["patterns"])
    nobody = make_tools(enriched_repo, "nobody")
    hidden = nobody.similar_queries("customer segment balance account")
    assert hidden["count"] == 0 and hidden["hidden_for_access"] >= 1
    assert "error" in alice.similar_queries("")
    # Vector search always returns neighbours; the limit still holds.
    assert alice.similar_queries("zzzz qqqq", limit=2)["count"] <= 2


def test_validate_sql_registry_and_access_checks(enriched_repo: SqliteCatalogRepository) -> None:
    tools = make_tools(enriched_repo)
    assert tools.validate_sql("DELETE FROM cos.retail_ods.account")["status"] == "rejected"
    assert tools.validate_sql("")["status"] == "rejected"
    ok = tools.validate_sql(GOOD_SQL)
    assert ok["status"] == "ok" and ok["plan"] and "Fragment" in ok["plan"]
    assert tools.validation_of("  " + GOOD_SQL + " ;")  # whitespace-insensitive identity
    bad = tools.validate_sql("SELECT nope FROM cos.retail_ods.account")
    assert bad["status"] == "semantic_error" and bad["error_name"] == "COLUMN_NOT_FOUND"
    denied = tools.validate_sql("SELECT customer_id FROM cos.risk_dm.customer_risk_score")
    assert denied["status"] == "permission_denied"

    assert tools.check_access(ACCOUNT)["access"] == "allowed"
    assert tools.check_access(RISK)["access"] == "denied"
    assert tools.check_access("cos.x.y")["access"] == "unknown"
    assert tools.check_access(RISK)["access"] == "denied"  # cached


def test_check_access_never_allows_on_errors(enriched_repo: SqliteCatalogRepository) -> None:
    tools = make_tools(enriched_repo)

    class Broken:
        def __call__(self) -> Any:
            raise RuntimeError("boom")

    tools.client = EndUserClient(Broken(), max_rows=10)
    assert tools.check_access(ACCOUNT)["access"] == "unknown"

    class Exploding:
        max_rows = 10

        def explain(self, sql: str) -> Any:
            raise ValueError("unexpected")

    tools._access_cache.clear()
    tools.client = Exploding()  # type: ignore[assignment]
    assert tools.check_access(ACCOUNT)["access"] == "unknown"


def test_run_sample_requires_prior_validation(enriched_repo: SqliteCatalogRepository) -> None:
    tools = make_tools(enriched_repo)
    rejected = tools.run_sample(GOOD_SQL)
    assert isinstance(rejected, dict) and rejected["status"] == "rejected"
    tools.validate_sql(GOOD_SQL)
    result = tools.run_sample(GOOD_SQL)
    assert isinstance(result, SampleResult) and result.ok
    assert result.row_limit == tools.max_rows == 10 and result.row_count <= 10
    tools.validate_sql("SELECT nope FROM cos.retail_ods.account")
    failed = tools.run_sample("SELECT nope FROM cos.retail_ods.account")
    assert isinstance(failed, dict) and failed["status"] == "rejected"


def test_call_dispatch_returns_errors_as_text(enriched_repo: SqliteCatalogRepository) -> None:
    tools = make_tools(enriched_repo)
    assert "unknown tool" in tools.call("drop_everything", {})["error"]
    assert "not valid JSON" in tools.call("search_tables", {"__invalid_json__": True})["error"]
    assert "invalid arguments" in tools.call("search_tables", {"nope": 1})["error"]
    assert tools.call("check_access", {"table": ACCOUNT})["access"] == "allowed"

    def explode(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    tools.search_tables = explode  # type: ignore[method-assign]
    assert (
        tools.call("search_tables", {"query": "x"})["error"] == "search_tables failed: RuntimeError"
    )
    assert tools.candidate_info("cos.x.y") is None and tools.candidate_info(ACCOUNT) is not None


def test_quote_table() -> None:
    assert quote_table("cos.retail_ods.account") == '"cos"."retail_ods"."account"'
    assert quote_table('cos.we"ird.t') == '"cos"."we""ird"."t"'
    with pytest.raises(ValueError, match="three-part"):
        quote_table("account")
