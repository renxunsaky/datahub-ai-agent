"""A thin session API over the compiled graph, shared by the CLI and the web UI."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from data_navigator.agent.graph import AgentGraph
from data_navigator.agent.state import MODE_CHAT, AgentState

MODE_CONFIRM = "confirm_sample"


@dataclass
class AgentReply:
    mode: str
    text: str = ""
    sql: str | None = None
    tables: list[str] = field(default_factory=list)
    joins: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    explanation: str = ""
    plan: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    clarifying_questions: list[str] = field(default_factory=list)
    confidence: float | None = None
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Pending confirmation payload (mode == confirm_sample): sql, max_rows, user.
    pending: dict[str, Any] | None = None
    # Sample rows of this turn (shown to the person, never persisted).
    sample: dict[str, Any] | None = None

    @property
    def needs_confirmation(self) -> bool:
        return self.mode == MODE_CONFIRM

    @classmethod
    def from_state(cls, reply: dict[str, Any] | None, sample: dict[str, Any] | None) -> AgentReply:
        if reply is None:
            return cls(mode=MODE_CHAT, text="", sample=sample)
        return cls(
            mode=str(reply.get("mode", MODE_CHAT)),
            text=str(reply.get("text", "")),
            sql=reply.get("sql"),
            tables=list(reply.get("tables", [])),
            joins=list(reply.get("joins", [])),
            assumptions=list(reply.get("assumptions", [])),
            explanation=str(reply.get("explanation", "")),
            plan=reply.get("plan"),
            candidates=list(reply.get("candidates", [])),
            clarifying_questions=list(reply.get("clarifying_questions", [])),
            confidence=reply.get("confidence"),
            reasons=list(reply.get("reasons", [])),
            warnings=list(reply.get("warnings", [])),
            sample=sample,
        )


class AgentSession:
    """One conversation (one LangGraph thread) for one user."""

    def __init__(
        self,
        graph: AgentGraph,
        *,
        user: str,
        thread_id: str | None = None,
    ) -> None:
        self._graph = graph
        self.user = user
        self.thread_id = thread_id or uuid.uuid4().hex
        self._pending: dict[str, Any] | None = None

    @property
    def config(self) -> RunnableConfig:
        return {"configurable": {"thread_id": self.thread_id}}

    @property
    def pending(self) -> dict[str, Any] | None:
        """The sample awaiting confirmation, if any."""
        return self._pending

    def ask(self, question: str) -> AgentReply:
        if self._pending is not None:
            # A new question while a confirmation is open counts as declining it.
            self.resume(False)
        turn: dict[str, Any] = {
            "messages": [{"role": "user", "content": question}],
            "question": question,
            "tool_steps": 0,
            "validation_failures": 0,
            "first_validation": None,
            "final_only": False,
            "forced_candidates": False,
            "reply": None,
            "sample": None,
        }
        result = self._graph.invoke(cast("AgentState", turn), config=self.config)
        return self._to_reply(dict(result))

    def resume(self, approved: bool) -> AgentReply:
        if self._pending is None:
            msg = "nothing is waiting for confirmation"
            raise RuntimeError(msg)
        result = self._graph.invoke(Command(resume=bool(approved)), config=self.config)
        return self._to_reply(dict(result))

    def state(self) -> dict[str, Any]:
        """Current graph values (used by the evaluation harness)."""
        snapshot = self._graph.get_state(self.config)
        return dict(snapshot.values)

    def _to_reply(self, result: dict[str, Any]) -> AgentReply:
        interrupts = result.get("__interrupt__") or []
        if interrupts:
            payload = interrupts[0].value
            self._pending = dict(payload) if isinstance(payload, dict) else {"value": payload}
            return AgentReply(
                mode=MODE_CONFIRM,
                text="Run this SQL with your identity and return at most "
                f"{self._pending.get('max_rows')} rows?",
                sql=self._pending.get("sql"),
                pending=self._pending,
            )
        self._pending = None
        return AgentReply.from_state(result.get("reply"), result.get("sample"))
