"""Pydantic schema that constrains the model's structured output for a table card."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from data_navigator.catalog.records import CardDraft, KeyColumn


class KeyColumnOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=256)
    meaning: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(default="", max_length=500)


class TableCardOutput(BaseModel):
    """What the model must return (as a JSON object)."""

    model_config = ConfigDict(extra="ignore")

    purpose: str = Field(min_length=1, max_length=600)
    key_columns: list[KeyColumnOutput] = Field(default_factory=list, max_length=30)
    common_usage: str = Field(default="", max_length=1500)
    example_questions: list[str] = Field(default_factory=list, max_length=10)
    overall_confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("example_questions", "evidence")
    @classmethod
    def _strip_items(cls, items: list[str]) -> list[str]:
        cleaned = [item.strip() for item in items if isinstance(item, str) and item.strip()]
        return [item[:300] for item in cleaned]

    def to_draft(self) -> CardDraft:
        return CardDraft(
            purpose=self.purpose.strip(),
            key_columns=[
                KeyColumn(
                    name=column.name.strip(),
                    meaning=column.meaning.strip(),
                    confidence=column.confidence,
                    evidence=column.evidence.strip(),
                )
                for column in self.key_columns
            ],
            common_usage=self.common_usage.strip(),
            example_questions=list(self.example_questions),
            overall_confidence=self.overall_confidence,
            evidence=list(self.evidence),
        )


class CardOutputError(ValueError):
    """The model's reply is not a valid card (caller may retry)."""


def parse_card_output(text: str | None, *, known_columns: Iterable[str]) -> TableCardOutput:
    """Parse and validate the model reply; key columns must exist in the table."""
    if not text or not text.strip():
        msg = "empty reply"
        raise CardOutputError(msg)
    payload = _extract_json(text)
    try:
        card = TableCardOutput.model_validate(payload)
    except ValidationError as error:
        msg = f"invalid card: {error.error_count()} validation error(s)"
        raise CardOutputError(msg) from error
    allowed = {name.lower() for name in known_columns}
    unknown = [column.name for column in card.key_columns if column.name.lower() not in allowed]
    if unknown:
        msg = f"key_columns reference unknown columns: {', '.join(sorted(unknown))}"
        raise CardOutputError(msg)
    seen: set[str] = set()
    deduplicated: list[KeyColumnOutput] = []
    for column in card.key_columns:
        key = column.name.lower()
        if key not in seen:
            seen.add(key)
            deduplicated.append(column)
    card.key_columns = deduplicated
    return card


def _extract_json(text: str) -> Any:
    """Accept a bare JSON object or one wrapped in a Markdown code fence."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        msg = "reply contains no JSON object"
        raise CardOutputError(msg)
    try:
        return json.loads(candidate[start : end + 1])
    except ValueError as error:
        msg = "reply is not valid JSON"
        raise CardOutputError(msg) from error
