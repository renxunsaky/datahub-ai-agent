from __future__ import annotations

from typing import Any

import pytest

from data_navigator.agent.factory import build_agent_session
from data_navigator.agent.graph import render_reply
from data_navigator.agent.session import MODE_CONFIRM, AgentReply
from data_navigator.agent.state import FinalAnswer, sql_hash
from data_navigator.catalog.sqlite_repo import SqliteCatalogRepository
from data_navigator.llm.replay import ReplayEntry, ReplayLLM, response_from_dict
from data_navigator.starburst.end_user_client import (
    EndUserClient,
    SampleResult,
    build_end_user_client,
)
from tests.conftest import REPLAY_SCRIPT, fixture_settings, make_session, no_secrets

GOOD_SQL = (
    "SELECT c.segment_code, SUM(a.balance) AS total_balance FROM cos.retail_ods.account a "
    "JOIN cos.retail_ods.customer c ON a.customer_id = c.customer_id WHERE a.status = 'OPEN' "
    "AND a.dt = CURRENT_DATE GROUP BY c.segment_code"
)
BALANCE_QUESTION = "What is the total balance per customer segment today?"


def script(*responses: dict[str, Any], match: str | None = None) -> ReplayLLM:
    return ReplayLLM(
        entries=[ReplayEntry(match=match, responses=[response_from_dict(r) for r in responses])]
    )


def calls(*items: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    return {"tool_calls": [{"name": name, "arguments": args} for name, args in items]}


def final(**arguments: Any) -> dict[str, Any]:
    return calls(("final_answer", arguments))


def last_tool_message(session: Any) -> dict[str, Any]:
    messages = session.state()["messages"]
    return next(m for m in reversed(messages) if m["role"] == "tool")


# -- happy paths ---------------------------------------------------------------------


def test_sql_mode_requires_validation_and_sample_needs_confirmation(
    enriched_repo: SqliteCatalogRepository,
) -> None:
    session = make_session(enriched_repo, ReplayLLM.from_yaml(REPLAY_SCRIPT), user="alice")
    reply = session.ask(BALANCE_QUESTION)
    assert reply.mode == "sql" and reply.sql == GOOD_SQL and not reply.warnings
    assert reply.tables == ["cos.retail_ods.account", "cos.retail_ods.customer"]
    assert reply.plan and "Fragment 0" in reply.plan
    assert "Validated read-only SQL" in reply.text and GOOD_SQL in reply.text
    state = session.state()
    assert state["first_validation"] == "ok" and state["tool_steps"] == 5
    audit = enriched_repo.list_audit_entries()
    assert audit[0].validation_status == "ok" and audit[0].generated_sql is not None
    assert "'OPEN'" not in audit[0].generated_sql and audit[0].details["mode"] == "sql"

    pending = session.ask("show me a sample please")
    assert pending.needs_confirmation and pending.mode == MODE_CONFIRM
    assert pending.sql == GOOD_SQL and pending.pending is not None
    assert pending.pending["max_rows"] == 10 and pending.pending["user"] == "alice"
    assert session.pending is not None
    assert not any(e.sample_executed for e in enriched_repo.list_audit_entries())

    done = session.resume(True)
    assert done.mode == "chat" and "sample" in done.text.lower()
    assert done.sample is not None and done.sample["status"] == "ok"
    assert done.sample["columns"] == ["segment_code", "total_balance"]
    assert 0 < done.sample["row_count"] <= 10 and done.sample["row_limit"] == 10
    tool_message = last_tool_message(session)
    assert '"rows"' not in tool_message["content"]
    assert "send_to_llm=false" in tool_message["content"]
    executed = [e for e in enriched_repo.list_audit_entries() if e.sample_confirmed is not None]
    assert executed[0].sample_confirmed and executed[0].sample_executed
    assert executed[0].sample_row_count == done.sample["row_count"]
    assert "<segment_code" not in (executed[0].generated_sql or "")


def test_declined_sample_never_runs(enriched_repo: SqliteCatalogRepository) -> None:
    session = make_session(enriched_repo, ReplayLLM.from_yaml(REPLAY_SCRIPT), user="alice")
    session.ask(BALANCE_QUESTION)
    assert session.ask("show me a sample").needs_confirmation
    reply = session.resume(False)
    assert reply.sample is None and reply.mode == "chat"
    assert "declined" in last_tool_message(session)["content"]
    entries = [e for e in enriched_repo.list_audit_entries() if e.sample_confirmed is not None]
    assert entries and entries[0].sample_confirmed is False and not entries[0].sample_executed
    with pytest.raises(RuntimeError, match="nothing is waiting"):
        session.resume(True)


def test_new_question_while_pending_declines_the_sample(
    enriched_repo: SqliteCatalogRepository,
) -> None:
    session = make_session(enriched_repo, ReplayLLM.from_yaml(REPLAY_SCRIPT), user="alice")
    session.ask(BALANCE_QUESTION)
    assert session.ask("show me a sample").needs_confirmation
    reply = session.ask("Show me the risk of our customers")
    assert reply.mode == "candidates" and session.pending is None
    assert not any(e.sample_executed for e in enriched_repo.list_audit_entries())


def test_sample_rows_reach_the_model_only_when_allowed(
    enriched_repo: SqliteCatalogRepository,
) -> None:
    settings = fixture_settings()
    settings.samples.send_to_llm = True
    settings.samples.max_rows = 2
    session = make_session(
        enriched_repo, ReplayLLM.from_yaml(REPLAY_SCRIPT), user="alice", settings=settings
    )
    session.ask(BALANCE_QUESTION)
    session.ask("show me a sample")
    reply = session.resume(True)
    assert reply.sample is not None and reply.sample["row_limit"] == 2
    assert '"rows"' in last_tool_message(session)["content"]


def test_candidates_mode_annotates_access_and_purpose(
    enriched_repo: SqliteCatalogRepository,
) -> None:
    session = make_session(enriched_repo, ReplayLLM.from_yaml(REPLAY_SCRIPT), user="bob")
    reply = session.ask("Show me the risk of our customers")
    assert reply.mode == "candidates" and reply.sql is None
    tables = [c["table"] for c in reply.candidates]
    assert tables == ["cos.risk_dm.customer_risk_score", "cos.risk_dm.daily_exposure"]
    assert all(c["access"] == "denied" for c in reply.candidates)  # bob only reads retail_ods
    assert reply.candidates[0]["purpose"] and "probability" in reply.candidates[0]["why"]
    assert reply.clarifying_questions and "AI inferred" in reply.text
    assert "access: denied" in reply.text


def test_validation_error_is_fixed_on_retry(enriched_repo: SqliteCatalogRepository) -> None:
    session = make_session(enriched_repo, ReplayLLM.from_yaml(REPLAY_SCRIPT), user="bob")
    reply = session.ask("Debit volume per channel since October")
    assert reply.mode == "sql" and reply.sql is not None and "GROUP BY channel" in reply.sql
    state = session.state()
    assert state["first_validation"] == "semantic_error" and state["validation_failures"] == 1


# -- enforcement -------------------------------------------------------------------------


def test_sql_mode_without_validation_is_downgraded(enriched_repo: SqliteCatalogRepository) -> None:
    llm = script(final(mode="sql", sql=GOOD_SQL, tables=["cos.retail_ods.account"]))
    reply = make_session(enriched_repo, llm).ask("balances")
    assert reply.mode == "candidates" and reply.sql is None
    assert any("did not pass validate_sql" in w for w in reply.warnings)
    assert [c["table"] for c in reply.candidates] == ["cos.retail_ods.account"]


def test_sql_mode_with_failed_validation_is_downgraded(
    enriched_repo: SqliteCatalogRepository,
) -> None:
    bad = "SELECT nope FROM cos.retail_ods.account"
    llm = script(calls(("validate_sql", {"sql": bad})), final(mode="sql", sql=bad))
    reply = make_session(enriched_repo, llm).ask("nope")
    assert reply.mode == "candidates" and reply.warnings


def test_retry_limit_forces_candidates(enriched_repo: SqliteCatalogRepository) -> None:
    settings = fixture_settings()
    settings.agent.max_sql_retries = 1
    bad = "SELECT nope FROM cos.retail_ods.account"
    llm = script(
        calls(("search_tables", {"query": "account"})),
        calls(("validate_sql", {"sql": bad})),
        calls(("validate_sql", {"sql": bad})),
        calls(("validate_sql", {"sql": bad})),  # ignored: only final_answer is allowed now
        final(mode="sql", sql=GOOD_SQL),
    )
    session = make_session(enriched_repo, llm, settings=settings)
    reply = session.ask("nope")
    assert reply.mode == "candidates"
    assert any("limit reached" in w or "retries exhausted" in w for w in reply.warnings)
    assert reply.candidates and reply.candidates[0]["table"].startswith("cos.")
    state = session.state()
    assert state["forced_candidates"] and state["validation_failures"] == 2
    prompts = llm.calls[-1]
    assert prompts[-1]["role"] == "system" and "could not be validated" in prompts[-1]["content"]


def test_tool_step_limit(enriched_repo: SqliteCatalogRepository) -> None:
    settings = fixture_settings()
    settings.agent.max_tool_steps = 2
    llm = script(
        calls(("search_tables", {"query": "account"}), ("search_tables", {"query": "customer"})),
        final(mode="candidates", candidates=[{"table": "cos.retail_ods.account", "why": "x"}]),
    )
    session = make_session(enriched_repo, llm, settings=settings)
    reply = session.ask("anything")
    assert reply.mode == "candidates" and reply.candidates[0]["why"] == "x"
    assert session.state()["tool_steps"] == 2 and len(llm.calls) == 2
    assert "all available tool steps" in llm.calls[-1][-1]["content"]


def test_step_limit_without_final_answer_lists_tables_seen(
    enriched_repo: SqliteCatalogRepository,
) -> None:
    settings = fixture_settings()
    settings.agent.max_tool_steps = 1
    llm = script(calls(("search_tables", {"query": "customer balance"})), {"content": "hmm"})
    reply = make_session(enriched_repo, llm, settings=settings).ask("x")
    assert reply.mode == "candidates" and reply.candidates
    assert any("limit reached" in w for w in reply.warnings) and reply.explanation == "hmm"


def test_run_sample_without_validation_is_refused_without_interrupt(
    enriched_repo: SqliteCatalogRepository,
) -> None:
    llm = script(calls(("run_sample", {"sql": GOOD_SQL})), {"content": "I cannot run it yet."})
    session = make_session(enriched_repo, llm)
    reply = session.ask("sample please")
    assert not reply.needs_confirmation and reply.mode == "chat"
    assert "validate it first" in last_tool_message(session)["content"]
    assert not any(e.sample_confirmed is not None for e in enriched_repo.list_audit_entries())


def test_run_sample_must_be_called_alone(enriched_repo: SqliteCatalogRepository) -> None:
    llm = script(
        calls(("validate_sql", {"sql": GOOD_SQL})),
        calls(
            ("check_access", {"table": "cos.retail_ods.account"}), ("run_sample", {"sql": GOOD_SQL})
        ),
        {"content": "ok"},
    )
    session = make_session(enriched_repo, llm)
    pending = session.ask("x")
    assert pending.needs_confirmation
    done = session.resume(True)
    assert done.sample is not None and done.sample["status"] == "ok"
    messages = session.state()["messages"]
    assert any(
        "must be the only call" in (m.get("content") or "") for m in messages if m["role"] == "tool"
    )


def test_run_sample_with_bad_arguments(enriched_repo: SqliteCatalogRepository) -> None:
    llm = script(calls(("run_sample", {"sql": 5})), {"content": "ok"})
    session = make_session(enriched_repo, llm)
    assert not session.ask("x").needs_confirmation
    assert "must be a string" in last_tool_message(session)["content"]


def test_sample_execution_failure_is_reported(enriched_repo: SqliteCatalogRepository) -> None:
    settings = fixture_settings()
    real = build_end_user_client(
        settings, no_secrets(), user="alice", schema_map=enriched_repo.schema_map()
    )

    class TimingOut(EndUserClient):
        def sample(self, sql: str, max_rows: int | None = None) -> SampleResult:
            return SampleResult(status="timeout", message="too slow", row_limit=10)

    client = TimingOut(real._connection_factory, max_rows=10)
    llm = script(
        calls(("validate_sql", {"sql": GOOD_SQL})),
        calls(("run_sample", {"sql": GOOD_SQL})),
        {"content": "the sample timed out"},
    )
    session = build_agent_session(
        settings, no_secrets(), enriched_repo, user="alice", llm=llm, client=client
    )
    assert session.ask("x").needs_confirmation
    reply = session.resume(True)
    assert reply.sample is not None and reply.sample["status"] == "timeout"
    assert reply.sample["message"] == "too slow" and reply.sample["rows"] == []
    entry = next(e for e in enriched_repo.list_audit_entries() if e.sample_confirmed is not None)
    assert entry.sample_confirmed and not entry.sample_executed and entry.sample_row_count is None


def test_plain_text_and_llm_errors_become_chat_replies(
    enriched_repo: SqliteCatalogRepository,
) -> None:
    reply = make_session(enriched_repo, script({"content": "Which month do you mean?"})).ask("hi")
    assert reply.mode == "chat" and reply.text == "Which month do you mean?"
    failing = make_session(enriched_repo, ReplayLLM(entries=[]))
    reply = failing.ask("hi")
    assert reply.mode == "chat" and "unavailable" in reply.text


def test_malformed_final_answer(enriched_repo: SqliteCatalogRepository) -> None:
    llm = script(final(mode="sql", candidates="not-a-list", confidence="high"))
    reply = make_session(enriched_repo, llm).ask("x")
    assert reply.mode == "candidates" and any("malformed" in w for w in reply.warnings)
    llm = script(calls(("final_answer", {"__invalid_json__": True})))
    reply = make_session(enriched_repo, llm).ask("x")
    assert reply.mode == "candidates"


def test_final_answer_with_other_calls_and_unknown_candidates(
    enriched_repo: SqliteCatalogRepository,
) -> None:
    llm = script(
        calls(
            ("search_tables", {"query": "x"}),
            ("final_answer", {"mode": "candidates", "candidates": [{"table": "cos.nope.x"}]}),
        )
    )
    session = make_session(enriched_repo, llm)
    reply = session.ask("x")
    assert reply.mode == "candidates" and reply.candidates == []
    assert "(none found)" in reply.text
    assert any(
        "no tools may accompany" in (m.get("content") or "") for m in session.state()["messages"]
    )


def test_history_window_never_starts_with_tool_messages(
    enriched_repo: SqliteCatalogRepository,
) -> None:
    settings = fixture_settings()
    settings.agent.history_window = 4
    llm = ReplayLLM.from_yaml(REPLAY_SCRIPT)
    session = make_session(enriched_repo, llm, settings=settings)
    session.ask(BALANCE_QUESTION)
    session.ask("Show me the risk of our customers")
    for prompt in llm.calls:
        assert prompt[0]["role"] == "system" and prompt[1]["role"] != "tool"
        assert len(prompt) <= 4 + 2


def test_render_reply_and_final_answer_defaults() -> None:
    assert FinalAnswer(mode="SQL").mode == "sql"
    assert FinalAnswer(mode="weird").mode == "candidates"
    assert FinalAnswer(confidence=7).confidence == 1.0
    assert sql_hash("SELECT 1;") == sql_hash("select   1")
    text = render_reply({"mode": "chat", "text": "hello", "warnings": ["w"]})
    assert text == "hello\nNote: w"
    reply = AgentReply.from_state(None, {"status": "ok"})
    assert reply.mode == "chat" and reply.sample == {"status": "ok"}
    sql_text = render_reply(
        {
            "mode": "sql",
            "sql": "SELECT 1",
            "tables": ["t"],
            "joins": ["j"],
            "assumptions": ["a"],
            "explanation": "e",
            "plan": "p",
            "warnings": [],
        }
    )
    assert "Tables: t" in sql_text and "EXPLAIN (excerpt):" in sql_text
