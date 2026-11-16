"""A scripted LLM for tests, demos and offline evaluation.

The script is a list of entries. For each call the client picks the first
entry whose ``match`` (a case-insensitive substring) appears in the most recent
user or tool message — or whose ``match`` is empty — and returns the entry's next
response.  Each response is either plain text (``content``), a JSON object
(``json``), or tool calls (``tool_calls``).  ``embed`` returns deterministic
hash-based vectors so vector search can be exercised offline.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from data_navigator.llm.client import ChatResponse, LLMError, Message, ToolCall, ToolSpec, Usage


@dataclass
class ReplayEntry:
    match: str | None
    responses: list[ChatResponse]
    cursor: int = 0
    loop: bool = True

    def next_response(self) -> ChatResponse:
        if not self.responses:
            msg = "replay entry has no responses"
            raise LLMError(msg)
        if self.cursor >= len(self.responses):
            if not self.loop:
                msg = "replay entry exhausted"
                raise LLMError(msg)
            self.cursor = len(self.responses) - 1
        response = self.responses[self.cursor]
        self.cursor += 1
        return response


@dataclass
class ReplayLLM:
    entries: list[ReplayEntry] = field(default_factory=list)
    name: str = "replay"
    embeddings: bool = True
    dimensions: int = 32
    calls: list[list[dict[str, Any]]] = field(default_factory=list)

    @property
    def model_name(self) -> str:
        return self.name

    @property
    def supports_embeddings(self) -> bool:
        return self.embeddings

    def chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] | None = None,
        json_mode: bool = False,
        temperature: float | None = None,
    ) -> ChatResponse:
        self.calls.append([dict(m) for m in messages])
        haystack = _latest_input(messages).lower()
        for entry in self.entries:
            if entry.match is None or entry.match.lower() in haystack:
                return entry.next_response()
        msg = "replay script has no entry for this input"
        raise LLMError(msg)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.embeddings:
            msg = "replay client has embeddings disabled"
            raise LLMError(msg)
        return [hash_embedding(text, self.dimensions) for text in texts]

    # -- construction ------------------------------------------------------------------

    @classmethod
    def from_yaml(cls, path: Path) -> ReplayLLM:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            msg = f"{path}: expected a mapping at the top level"
            raise ValueError(msg)
        entries = [
            ReplayEntry(
                match=item.get("match"),
                responses=[response_from_dict(r) for r in item.get("responses", [])],
                loop=bool(item.get("loop", True)),
            )
            for item in raw.get("entries", [])
        ]
        return cls(
            entries=entries,
            name=str(raw.get("model", "replay")),
            embeddings=bool(raw.get("embeddings", True)),
        )


def response_from_dict(data: Mapping[str, Any]) -> ChatResponse:
    """Build a response from a ``content``, ``json`` or ``tool_calls`` mapping."""
    if "tool_calls" in data:
        calls = [
            ToolCall(
                id=str(call.get("id") or f"call_{i}"),
                name=str(call["name"]),
                arguments=dict(call.get("arguments") or {}),
            )
            for i, call in enumerate(data["tool_calls"])
        ]
        return ChatResponse(
            content=data.get("content"), tool_calls=calls, finish_reason="tool_calls"
        )
    if "json" in data:
        return ChatResponse(content=json.dumps(data["json"]), finish_reason="stop")
    return ChatResponse(content=str(data.get("content", "")), finish_reason="stop", usage=Usage())


def _latest_input(messages: Sequence[Message]) -> str:
    """The newest user/tool message content, used to pick a replay entry."""
    for message in reversed(messages):
        if message.get("role") in {"user", "tool"}:
            content = message.get("content")
            return content if isinstance(content, str) else json.dumps(content)
    return ""


def hash_embedding(text: str, dimensions: int) -> list[float]:
    """A deterministic bag-of-words vector (no model involved)."""
    vector = [0.0] * dimensions
    for token in text.lower().split():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else vector
