"""Graph state and the structured final answer."""

from __future__ import annotations

import hashlib
import operator
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator

MODE_SQL = "sql"
MODE_CANDIDATES = "candidates"
MODE_CHAT = "chat"


class AgentState(TypedDict, total=False):
    # Full OpenAI-style conversation (system prompt excluded); appended, never replaced.
    messages: Annotated[list[dict[str, Any]], operator.add]
    # Per-turn fields (overwritten at the start of every turn).
    question: str
    tool_steps: int
    validation_failures: int
    first_validation: str | None
    final_only: bool
    forced_candidates: bool
    reply: dict[str, Any] | None
    sample: dict[str, Any] | None
    # Per-session fields.
    validated: dict[str, dict[str, Any]]
    tables_seen: list[str]


def sql_hash(sql: str) -> str:
    """Identity of a statement for the validated-SQL registry (whitespace-insensitive)."""
    canonical = " ".join(sql.split()).strip().rstrip(";").strip().lower()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Candidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    table: str = Field(min_length=1)
    why: str = ""
    uncertainty: str = ""


class FinalAnswer(BaseModel):
    """Arguments of the ``final_answer`` tool (validated before use)."""

    model_config = ConfigDict(extra="ignore")

    mode: str = MODE_CANDIDATES
    sql: str | None = None
    tables: list[str] = Field(default_factory=list)
    joins: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    explanation: str = ""
    candidates: list[Candidate] = Field(default_factory=list)
    clarifying_questions: list[str] = Field(default_factory=list)
    confidence: float | None = None
    reasons: list[str] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        lowered = value.strip().lower()
        return lowered if lowered in {MODE_SQL, MODE_CANDIDATES} else MODE_CANDIDATES

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return max(0.0, min(1.0, value))
