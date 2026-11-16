"""The LangGraph state machine.

::

    START -> llm -> (tools -> llm)* -> finalize -> END
                 \\-> sample (interrupt, then run) -> llm

* ``llm`` calls the model with the tool specs (or only ``final_answer`` once a
  limit is hit).
* ``tools`` runs catalog / validation tools; every error is returned as text.
* ``sample`` handles ``run_sample``: the SQL must have passed ``validate_sql``
  (hash check), then ``interrupt`` pauses until the person confirms; only then
  does the row-capped query run.  Rows go to the UI through the state, and to
  the model only when ``samples.send_to_llm`` is true.
* ``finalize`` turns ``final_answer`` into a reply, enforcing in code that
  mode ``sql`` is only possible for validated SQL, that forced candidates mode
  cannot be bypassed, and annotating candidates with deterministic access checks.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from data_navigator.agent.prompts import (
    FINAL_ONLY_NOTE,
    FORCED_CANDIDATES_NOTE,
    SYSTEM_PROMPT,
    tool_result_block,
)
from data_navigator.agent.state import (
    MODE_CANDIDATES,
    MODE_CHAT,
    MODE_SQL,
    AgentState,
    FinalAnswer,
)
from data_navigator.agent.tools import (
    ALL_TOOL_SPECS,
    FINAL_ANSWER,
    FINAL_ANSWER_SPEC,
    RUN_SAMPLE,
    AgentTools,
    tables_in,
)
from data_navigator.audit.log import AuditLogger
from data_navigator.config import AgentSettings, SampleSettings
from data_navigator.llm.client import LLMClient, LLMError, ToolCall
from data_navigator.sqlproc.readonly_guard import readonly_guard
from data_navigator.starburst.end_user_client import SampleResult

log = logging.getLogger(__name__)

NODE_LLM = "llm"
NODE_TOOLS = "tools"
NODE_SAMPLE = "sample"
NODE_FINALIZE = "finalize"

_FAILED_VALIDATION = {"rejected", "syntax_error", "semantic_error", "permission_denied"}


@dataclass
class AgentRuntime:
    llm: LLMClient
    tools: AgentTools
    settings: AgentSettings
    samples: SampleSettings
    audit: AuditLogger
    user: str


AgentGraph = CompiledStateGraph[AgentState, None, AgentState, AgentState]
# ``Callable[..., ...]`` (not ``Callable[[AgentState], ...]``) so the functions satisfy
# LangGraph's node protocols, whose ``state`` parameter may be passed by keyword.
NodeFn = Callable[..., dict[str, Any]]


def build_graph(
    runtime: AgentRuntime, *, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> AgentGraph:
    graph = StateGraph(AgentState)
    graph.add_node(NODE_LLM, _llm_node(runtime))
    graph.add_node(NODE_TOOLS, _tools_node(runtime))
    graph.add_node(NODE_SAMPLE, _sample_node(runtime))
    graph.add_node(NODE_FINALIZE, _finalize_node(runtime))
    graph.add_edge(START, NODE_LLM)
    graph.add_conditional_edges(
        NODE_LLM,
        _route_after_llm,
        {NODE_TOOLS: NODE_TOOLS, NODE_SAMPLE: NODE_SAMPLE, NODE_FINALIZE: NODE_FINALIZE},
    )
    graph.add_edge(NODE_TOOLS, NODE_LLM)
    graph.add_edge(NODE_SAMPLE, NODE_LLM)
    graph.add_edge(NODE_FINALIZE, END)
    return graph.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def _llm_node(runtime: AgentRuntime) -> NodeFn:
    def node(state: AgentState) -> dict[str, Any]:
        history = _window(state.get("messages", []), runtime.settings.history_window)
        prompt: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
        tools: Sequence[dict[str, Any]] = ALL_TOOL_SPECS
        if state.get("final_only"):
            note = FORCED_CANDIDATES_NOTE if state.get("forced_candidates") else FINAL_ONLY_NOTE
            prompt.append({"role": "system", "content": note})
            tools = [FINAL_ANSWER_SPEC]
        try:
            response = runtime.llm.chat(prompt, tools=tools)
        except LLMError as error:
            log.error("model call failed: %s", type(error).__name__)
            return {
                "messages": [
                    {
                        "role": "assistant",
                        "content": "The language model is unavailable right now "
                        f"({type(error).__name__}); please try again later.",
                    }
                ]
            }
        message = response.to_message()
        if state.get("final_only") and any(
            call.name != FINAL_ANSWER for call in response.tool_calls
        ):
            # The model ignored the restriction: drop the stray calls, keep final_answer.
            kept = [c for c in response.tool_calls if c.name == FINAL_ANSWER]
            message["tool_calls"] = [c.to_message_part() for c in kept]
            if not kept:
                message.pop("tool_calls", None)
                message["content"] = message.get("content") or ""
        return {"messages": [message]}

    return node


def _route_after_llm(state: AgentState) -> str:
    calls = _tool_calls(state["messages"][-1])
    names = {call.name for call in calls}
    if FINAL_ANSWER in names:
        return NODE_FINALIZE
    if RUN_SAMPLE in names:
        return NODE_SAMPLE
    if calls:
        return NODE_TOOLS
    return NODE_FINALIZE


def _tools_node(runtime: AgentRuntime) -> NodeFn:
    def node(state: AgentState) -> dict[str, Any]:
        calls = _tool_calls(state["messages"][-1])
        steps = state.get("tool_steps", 0)
        failures = state.get("validation_failures", 0)
        first_validation = state.get("first_validation")
        seen = list(state.get("tables_seen", []))
        messages: list[dict[str, Any]] = []
        for call in calls:
            steps += 1
            if steps > runtime.settings.max_tool_steps:
                result: dict[str, Any] = {
                    "error": "tool step limit reached for this turn; call final_answer"
                }
            else:
                result = runtime.tools.call(call.name, call.arguments)
            if call.name == "validate_sql":
                status = str(result.get("status", "unknown"))
                if first_validation is None:
                    first_validation = status
                if status != "ok":
                    failures += 1
            if call.name == "search_tables":
                for name in tables_in([result]):
                    if name not in seen:
                        seen.append(name)
            messages.append(_tool_message(call, result))
        forced = failures > runtime.settings.max_sql_retries
        final_only = forced or steps >= runtime.settings.max_tool_steps
        return {
            "messages": messages,
            "tool_steps": steps,
            "validation_failures": failures,
            "first_validation": first_validation,
            "final_only": final_only,
            "forced_candidates": forced or bool(state.get("forced_candidates")),
            "tables_seen": seen,
        }

    return node


def _sample_node(runtime: AgentRuntime) -> NodeFn:
    def node(state: AgentState) -> dict[str, Any]:
        calls = _tool_calls(state["messages"][-1])
        messages: list[dict[str, Any]] = []
        sample_call: ToolCall | None = None
        for call in calls:
            if call.name == RUN_SAMPLE and sample_call is None:
                sample_call = call
            else:
                messages.append(
                    _tool_message(call, {"error": "run_sample must be the only call in a message"})
                )
        assert sample_call is not None
        sql = sample_call.arguments.get("sql")
        question = state.get("question", "")
        if not isinstance(sql, str) or not sql.strip():
            messages.append(_tool_message(sample_call, {"error": "sql must be a string"}))
            return {"messages": messages}
        record = runtime.tools.validation_of(sql)
        if record is None or record.status != "ok" or readonly_guard(sql).rejected:
            messages.append(
                _tool_message(
                    sample_call,
                    {
                        "status": "rejected",
                        "message": "run_sample only accepts SQL that passed validate_sql "
                        "in this session; validate it first",
                    },
                )
            )
            return {"messages": messages}

        # Pause here. The node is re-run from the top on resume, which is why nothing
        # with side effects happens before this point.
        decision = interrupt(
            {
                "type": "confirm_sample",
                "sql": sql,
                "max_rows": runtime.tools.max_rows,
                "user": runtime.user,
            }
        )
        approved = _approved(decision)
        if not approved:
            runtime.audit.record(
                user=runtime.user,
                question=question,
                generated_sql=sql,
                validation_status=record.status,
                sample_confirmed=False,
                sample_executed=False,
                details={"event": "sample_declined"},
            )
            messages.append(
                _tool_message(
                    sample_call,
                    {"status": "declined", "message": "the user declined to run the sample"},
                )
            )
            return {"messages": messages, "sample": None}

        outcome = runtime.tools.run_sample(sql)
        sample_state: dict[str, Any]
        if isinstance(outcome, SampleResult):
            sample_state = {
                "sql": sql,
                "status": outcome.status,
                "message": outcome.message,
                "columns": list(outcome.columns),
                "rows": [list(row) for row in outcome.rows],
                "row_count": outcome.row_count,
                "row_limit": outcome.row_limit,
            }
            for_model: dict[str, Any] = {
                "status": outcome.status,
                "message": outcome.message,
                "columns": list(outcome.columns),
                "row_count": outcome.row_count,
                "row_limit": outcome.row_limit,
            }
            if runtime.samples.send_to_llm:
                for_model["rows"] = [list(row) for row in outcome.rows]
            else:
                for_model["note"] = "rows are shown to the user only (samples.send_to_llm=false)"
        else:
            sample_state = {"sql": sql, "status": str(outcome.get("status", "rejected"))}
            sample_state["message"] = outcome.get("message")
            for_model = dict(outcome)
        runtime.audit.record(
            user=runtime.user,
            question=question,
            generated_sql=sql,
            validation_status=record.status,
            sample_confirmed=True,
            sample_executed=sample_state["status"] == "ok",
            sample_row_count=sample_state.get("row_count"),
            details={"event": "sample", "status": sample_state["status"]},
        )
        messages.append(_tool_message(sample_call, for_model))
        return {"messages": messages, "sample": sample_state}

    return node


def _finalize_node(runtime: AgentRuntime) -> NodeFn:
    def node(state: AgentState) -> dict[str, Any]:
        last = state["messages"][-1]
        calls = _tool_calls(last)
        question = state.get("question", "")
        messages: list[dict[str, Any]] = []
        warnings: list[str] = []
        final_call = next((c for c in calls if c.name == FINAL_ANSWER), None)
        if final_call is None and not state.get("final_only"):
            text = last.get("content") or ""
            chat_reply: dict[str, Any] = {"mode": MODE_CHAT, "text": text, "warnings": []}
            runtime.audit.record(
                user=runtime.user,
                question=question,
                details={"mode": MODE_CHAT, "tool_steps": state.get("tool_steps", 0)},
            )
            return {"reply": chat_reply, "messages": []}

        for call in calls:
            if call is not final_call:
                messages.append(
                    _tool_message(call, {"error": "no tools may accompany final_answer"})
                )
        if final_call is None:
            # Limits were hit and the model still did not call final_answer: answer for it.
            answer = FinalAnswer(
                mode=MODE_CANDIDATES,
                explanation=last.get("content")
                or "The tool-step or validation limit was reached before an answer was ready.",
            )
            warnings.append("limit reached; listing the tables seen so far")
        else:
            try:
                answer = FinalAnswer.model_validate(final_call.arguments)
            except ValidationError as error:
                answer = FinalAnswer(explanation="the model produced a malformed final answer")
                warnings.append(f"malformed final answer ({error.error_count()} field errors)")
        warnings: list[str] = []
        try:
            answer = FinalAnswer.model_validate(final_call.arguments)
        except ValidationError as error:
            answer = FinalAnswer(explanation="the model produced a malformed final answer")
            warnings.append(f"malformed final answer ({error.error_count()} field errors)")

        mode = answer.mode
        validation: dict[str, Any] | None = None
        plan: str | None = None
        sql = answer.sql.strip() if answer.sql else None
        if mode == MODE_SQL:
            record = runtime.tools.validation_of(sql) if sql else None
            if not sql or record is None or record.status != "ok" or readonly_guard(sql).rejected:
                warnings.append(
                    "the SQL did not pass validate_sql in this session; showing candidates instead"
                )
                mode = MODE_CANDIDATES
            else:
                validation = record.to_dict()
                plan = record.plan
        if state.get("forced_candidates") and mode == MODE_SQL:
            warnings.append("validation retries exhausted; showing candidates instead")
            mode = MODE_CANDIDATES

        candidates: list[dict[str, Any]] = []
        if mode == MODE_CANDIDATES:
            names = [c.table for c in answer.candidates]
            if not names:
                names = list(answer.tables) or list(state.get("tables_seen", []))
            notes = {c.table.lower(): c for c in answer.candidates}
            for name in names:
                info = runtime.tools.candidate_info(name)
                if info is None:
                    continue
                note = notes.get(name.lower())
                info["why"] = note.why if note else ""
                info["uncertainty"] = note.uncertainty if note else ""
                info["access"] = runtime.tools.check_access(name)["access"]
                candidates.append(info)

        reply: dict[str, Any] = {
            "mode": mode,
            "sql": sql if mode == MODE_SQL else None,
            "tables": list(answer.tables),
            "joins": list(answer.joins),
            "assumptions": list(answer.assumptions),
            "explanation": answer.explanation,
            "validation": validation,
            "plan": plan,
            "candidates": candidates,
            "clarifying_questions": list(answer.clarifying_questions),
            "confidence": answer.confidence,
            "reasons": list(answer.reasons),
            "warnings": warnings,
            "text": "",
        }
        reply["text"] = render_reply(reply)
        if final_call is not None:
            messages.append(
                _tool_message(
                    final_call, {"status": "delivered", "mode": mode, "warnings": warnings}
                )
            )
        last_validation = runtime.tools.validation_of(sql) if sql else None
        runtime.audit.record(
            user=runtime.user,
            question=question,
            generated_sql=sql,
            validation_status=last_validation.status if last_validation else None,
            details={
                "mode": mode,
                "tables": list(answer.tables) or [c["table"] for c in candidates],
                "confidence": answer.confidence,
                "reasons": list(answer.reasons),
                "warnings": warnings,
                "tool_steps": state.get("tool_steps", 0),
                "validation_failures": state.get("validation_failures", 0),
                "first_validation": state.get("first_validation"),
                "forced_candidates": bool(state.get("forced_candidates")),
            },
        )
        return {"reply": reply, "messages": messages}

    return node


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def render_reply(reply: dict[str, Any]) -> str:
    """Plain-text rendering shared by the CLI and the web UI."""
    lines: list[str] = []
    mode = reply.get("mode")
    if mode == MODE_SQL:
        lines.append("Validated read-only SQL (EXPLAIN passed with your identity):")
        lines.append("")
        lines.append(reply.get("sql") or "")
        lines.append("")
        if reply.get("tables"):
            lines.append("Tables: " + ", ".join(reply["tables"]))
        if reply.get("joins"):
            lines.append("Joins: " + "; ".join(reply["joins"]))
        if reply.get("assumptions"):
            lines.append("Assumptions: " + "; ".join(reply["assumptions"]))
        if reply.get("explanation"):
            lines.append(reply["explanation"])
        if reply.get("plan"):
            lines.append("")
            lines.append("EXPLAIN (excerpt):")
            lines.append(reply["plan"])
        lines.append("")
        lines.append("Ask for a sample to see a few rows (you will be asked to confirm).")
    elif mode == MODE_CANDIDATES:
        lines.append("I am not confident enough to write SQL yet. Possible tables:")
        for candidate in reply.get("candidates", []):
            purpose = candidate.get("purpose") or "no description"
            source = candidate.get("purpose_source")
            if source and "ai_inferred" in source:
                purpose += " (AI inferred)"
            owner = candidate.get("owner_team") or "unknown owner"
            lines.append(
                f"  - {candidate['table']} [{owner}; access: {candidate.get('access')}] {purpose}"
            )
            if candidate.get("why"):
                lines.append(f"      why: {candidate['why']}")
            if candidate.get("uncertainty"):
                lines.append(f"      uncertain: {candidate['uncertainty']}")
        if not reply.get("candidates"):
            lines.append("  (none found)")
        if reply.get("explanation"):
            lines.append(reply["explanation"])
        for question in reply.get("clarifying_questions", []):
            lines.append(f"Question: {question}")
    else:
        lines.append(reply.get("text") or "")
    for warning in reply.get("warnings", []):
        lines.append(f"Note: {warning}")
    return "\n".join(lines).strip()


def _tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for raw in message.get("tool_calls") or []:
        function = raw.get("function") or {}
        arguments = function.get("arguments") or "{}"
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except ValueError:
                parsed = {"__invalid_json__": True}
        else:
            parsed = dict(arguments)
        if not isinstance(parsed, dict):
            parsed = {"__invalid_json__": True}
        calls.append(
            ToolCall(id=str(raw.get("id")), name=str(function.get("name")), arguments=parsed)
        )
    return calls


def _tool_message(call: ToolCall, payload: Any) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": tool_result_block(call.name, payload),
    }


def _window(messages: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    if len(messages) <= size:
        return messages
    window = messages[-size:]
    # Never start with orphaned tool results: drop until a non-tool message.
    while window and window[0].get("role") == "tool":
        window = window[1:]
    return window


def _approved(decision: Any) -> bool:
    if isinstance(decision, bool):
        return decision
    if isinstance(decision, dict):
        return bool(decision.get("approved", False))
    return False
