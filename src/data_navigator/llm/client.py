"""Thin LLM client.

* :class:`LLMClient` is the protocol the rest of the code depends on.
* :class:`OpenAICompatibleClient` talks to an OpenAI-compatible endpoint
  (``/chat/completions`` and ``/embeddings``).  ``base_url`` comes from the
  configuration and has no default, so the application can never fall back to a
  public service.

Retries use exponential backoff on transport errors, timeouts, HTTP 429 and
5xx.  A simple token bucket enforces ``requests_per_minute`` and a semaphore
enforces ``max_concurrency``.  Logs contain token counts and latency only.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

from data_navigator.config import LLMSettings

log = logging.getLogger(__name__)

Message = Mapping[str, Any]
ToolSpec = Mapping[str, Any]


class LLMError(RuntimeError):
    """The model could not be called (after retries) or returned an unusable reply."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def to_message_part(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": json.dumps(self.arguments)},
        }


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class ChatResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: Usage = Usage()
    model: str | None = None

    def to_message(self) -> dict[str, Any]:
        """The assistant message to append to the conversation."""
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [call.to_message_part() for call in self.tool_calls]
        return message


class LLMClient(Protocol):
    """What the cards generator and the agent need from a model."""

    @property
    def model_name(self) -> str: ...

    @property
    def supports_embeddings(self) -> bool: ...

    def chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] | None = None,
        json_mode: bool = False,
        temperature: float | None = None,
    ) -> ChatResponse: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TokenBucket:
    """A minimal thread-safe token bucket (``rate`` tokens per minute)."""

    def __init__(self, per_minute: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._capacity = float(per_minute)
        self._tokens = float(per_minute)
        self._rate = per_minute / 60.0
        self._clock = clock
        self._updated = clock()
        self._lock = threading.Lock()

    def wait_time(self) -> float:
        """Consume a token if available; otherwise return how long to sleep."""
        with self._lock:
            now = self._clock()
            self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
            self._updated = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            return (1.0 - self._tokens) / self._rate


# ---------------------------------------------------------------------------
# OpenAI-compatible client
# ---------------------------------------------------------------------------

_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenAICompatibleClient:
    def __init__(
        self,
        settings: LLMSettings,
        api_key: SecretStr | None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not settings.base_url:
            msg = "llm.base_url is not configured (no default on purpose)"
            raise LLMError(msg)
        if not settings.model:
            msg = "llm.model is not configured"
            raise LLMError(msg)
        self._settings = settings
        self._model: str = settings.model
        headers = {"Content-Type": "application/json"}
        if api_key is not None and api_key.get_secret_value():
            headers["Authorization"] = f"Bearer {api_key.get_secret_value()}"
        self._http = httpx.Client(
            base_url=settings.base_url.rstrip("/"),
            headers=headers,
            timeout=settings.timeout_s,
            transport=transport,
        )
        self._sleep = sleep
        self._bucket = TokenBucket(settings.requests_per_minute)
        self._semaphore = threading.BoundedSemaphore(settings.max_concurrency)

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def supports_embeddings(self) -> bool:
        return bool(self._settings.embedding_model)

    def close(self) -> None:
        self._http.close()

    # -- API -----------------------------------------------------------------------

    def chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] | None = None,
        json_mode: bool = False,
        temperature: float | None = None,
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [dict(message) for message in messages],
            "temperature": self._settings.temperature if temperature is None else temperature,
        }
        if tools:
            payload["tools"] = [dict(tool) for tool in tools]
        if json_mode and self._settings.json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self._post("/chat/completions", payload)
        return parse_chat_completion(data)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not self._settings.embedding_model:
            msg = "llm.embedding_model is not configured"
            raise LLMError(msg)
        if not texts:
            return []
        payload = {"model": self._settings.embedding_model, "input": list(texts)}
        data = self._post("/embeddings", payload)
        try:
            items = sorted(data["data"], key=lambda item: int(item.get("index", 0)))
            vectors = [[float(x) for x in item["embedding"]] for item in items]
        except (KeyError, TypeError, ValueError) as error:
            msg = "embeddings response is malformed"
            raise LLMError(msg) from error
        if len(vectors) != len(texts):
            msg = "embeddings response has the wrong number of vectors"
            raise LLMError(msg)
        return vectors

    # -- transport ---------------------------------------------------------------------

    def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        attempts = self._settings.max_retries + 1
        delay = 1.0
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            wait = self._bucket.wait_time()
            while wait > 0:
                self._sleep(wait)
                wait = self._bucket.wait_time()
            started = time.monotonic()
            with self._semaphore:
                try:
                    response = self._http.post(path, json=payload)
                except httpx.HTTPError as error:
                    last_error = error
                    log.warning(
                        "llm call failed (%s), attempt %d/%d",
                        type(error).__name__,
                        attempt,
                        attempts,
                    )
                else:
                    elapsed = time.monotonic() - started
                    if response.status_code in _RETRY_STATUS:
                        last_error = LLMError(f"HTTP {response.status_code}")
                        log.warning(
                            "llm call returned %d, attempt %d/%d",
                            response.status_code,
                            attempt,
                            attempts,
                        )
                    elif response.status_code >= 400:
                        msg = f"LLM endpoint returned HTTP {response.status_code}"
                        raise LLMError(msg)
                    else:
                        data = _json_body(response)
                        usage = data.get("usage") or {}
                        log.info(
                            "llm %s ok in %.2fs (prompt=%s completion=%s tokens)",
                            path,
                            elapsed,
                            usage.get("prompt_tokens", "?"),
                            usage.get("completion_tokens", "?"),
                        )
                        return data
            if attempt < attempts:
                self._sleep(delay)
                delay = min(delay * 2, 30.0)
        msg = f"LLM call failed after {attempts} attempt(s)"
        raise LLMError(msg) from last_error


def _json_body(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as error:
        msg = "LLM endpoint returned a non-JSON body"
        raise LLMError(msg) from error
    if not isinstance(data, dict):
        msg = "LLM endpoint returned an unexpected JSON shape"
        raise LLMError(msg)
    return data


def parse_chat_completion(data: Mapping[str, Any]) -> ChatResponse:
    """Turn a ``/chat/completions`` body into a :class:`ChatResponse`."""
    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as error:
        msg = "chat completion response is malformed"
        raise LLMError(msg) from error
    tool_calls: list[ToolCall] = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        function = raw.get("function") or {}
        arguments_raw = function.get("arguments") or "{}"
        try:
            arguments = json.loads(arguments_raw) if isinstance(arguments_raw, str) else {}
        except ValueError:
            arguments = {"__invalid_json__": True}
        if not isinstance(arguments, dict):
            arguments = {"__invalid_json__": True}
        tool_calls.append(
            ToolCall(
                id=str(raw.get("id") or f"call_{index}"),
                name=str(function.get("name") or ""),
                arguments=arguments,
            )
        )
    usage_raw = data.get("usage") or {}
    usage = Usage(
        prompt_tokens=int(usage_raw.get("prompt_tokens", 0) or 0),
        completion_tokens=int(usage_raw.get("completion_tokens", 0) or 0),
    )
    content = message.get("content")
    return ChatResponse(
        content=content if isinstance(content, str) else None,
        tool_calls=tool_calls,
        finish_reason=choice.get("finish_reason"),
        usage=usage,
        model=data.get("model"),
    )
