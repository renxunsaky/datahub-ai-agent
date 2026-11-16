from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from data_navigator.config import LLMSettings, Secrets, Settings
from data_navigator.llm.client import (
    LLMError,
    OpenAICompatibleClient,
    TokenBucket,
    parse_chat_completion,
)
from data_navigator.llm.factory import build_llm
from data_navigator.llm.replay import ReplayEntry, ReplayLLM, hash_embedding, response_from_dict

BASE = "https://llm.example.internal/v1"


def settings(**overrides: Any) -> LLMSettings:
    values: dict[str, Any] = {
        "provider": "openai_compatible",
        "base_url": BASE,
        "model": "chat-model",
        "embedding_model": "embed-model",
        "max_retries": 2,
        "requests_per_minute": 6000,
    }
    values.update(overrides)
    return LLMSettings(**values)


def make_client(
    handler: Any, llm: LLMSettings | None = None
) -> tuple[OpenAICompatibleClient, list[float]]:
    sleeps: list[float] = []
    client = OpenAICompatibleClient(
        llm or settings(),
        SecretStr("secret-key"),
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    return client, sleeps


def chat_body(
    content: str | None = "hi", tool_calls: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "model": "chat-model",
        "choices": [{"message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def test_chat_sends_openai_payload_and_parses_reply() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=chat_body("hello"))

    client, _ = make_client(handler)
    response = client.chat(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
        json_mode=True,
    )
    assert response.content == "hello" and response.usage.total_tokens == 15
    request = seen[0]
    assert str(request.url) == BASE + "/chat/completions"
    assert request.headers["authorization"] == "Bearer secret-key"
    payload = json.loads(request.content)
    assert payload["model"] == "chat-model" and payload["tools"][0]["function"]["name"] == "f"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0.0


def test_chat_parses_tool_calls_and_bad_arguments() -> None:
    body = chat_body(
        None,
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "search", "arguments": '{"q": 1}'},
            },
            {"id": "c2", "type": "function", "function": {"name": "x", "arguments": "{not json"}},
        ],
    )
    response = parse_chat_completion(body)
    assert [c.name for c in response.tool_calls] == ["search", "x"]
    assert response.tool_calls[0].arguments == {"q": 1}
    assert response.tool_calls[1].arguments == {"__invalid_json__": True}
    assert response.to_message()["tool_calls"][0]["function"]["name"] == "search"


def test_retries_on_429_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(200, json=chat_body("ok"))

    client, sleeps = make_client(handler)
    assert client.chat([{"role": "user", "content": "x"}]).content == "ok"
    assert calls["n"] == 2 and sleeps == [1.0]


def test_gives_up_after_max_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client, sleeps = make_client(handler)
    with pytest.raises(LLMError, match="after 3 attempt"):
        client.chat([{"role": "user", "content": "x"}])
    assert sleeps == [1.0, 2.0]


def test_client_errors_are_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad"})

    client, sleeps = make_client(handler)
    with pytest.raises(LLMError, match="HTTP 400"):
        client.chat([{"role": "user", "content": "x"}])
    assert sleeps == []


@pytest.mark.parametrize("body", ["not json", {"choices": []}, {"unexpected": True}])
def test_malformed_replies_raise(body: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(200, text=body)
        return httpx.Response(200, json=body)

    client, _ = make_client(handler)
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "x"}])


def test_embed_orders_vectors_by_index() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "embed-model" and payload["input"] == ["a", "b"]
        return httpx.Response(
            200,
            json={"data": [{"index": 1, "embedding": [0.5]}, {"index": 0, "embedding": [1.0]}]},
        )

    client, _ = make_client(handler)
    assert client.embed(["a", "b"]) == [[1.0], [0.5]]
    assert client.embed([]) == []


def test_embed_without_model_or_with_wrong_count_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    client, _ = make_client(handler, settings(embedding_model=None))
    assert not client.supports_embeddings
    with pytest.raises(LLMError, match="embedding_model"):
        client.embed(["a"])
    client, _ = make_client(handler)
    with pytest.raises(LLMError, match="wrong number"):
        client.embed(["a", "b"])


def test_base_url_and_model_are_required_and_internal() -> None:
    with pytest.raises(LLMError, match="base_url"):
        OpenAICompatibleClient(settings(base_url=None), None)
    with pytest.raises(LLMError, match=r"llm\.model"):
        OpenAICompatibleClient(settings(model=None), None)
    with pytest.raises(ValueError, match="internal LLMaaS"):
        LLMSettings(base_url="https://api.openai.com/v1")


def test_token_bucket_paces_requests() -> None:
    clock = {"t": 0.0}
    bucket = TokenBucket(60, clock=lambda: clock["t"])  # one per second
    for _ in range(60):
        assert bucket.wait_time() == 0.0
    assert bucket.wait_time() == pytest.approx(1.0)
    clock["t"] = 1.0
    assert bucket.wait_time() == 0.0


def test_factory_returns_none_replay_or_real(tmp_path: Path) -> None:
    secrets = Secrets(_env_file=None)  # type: ignore[call-arg]
    assert build_llm(Settings(), secrets) is None
    script = tmp_path / "replay.yaml"
    script.write_text("entries:\n  - match: null\n    responses:\n      - content: hi\n")
    replay_settings = Settings()
    replay_settings.llm.provider = "replay"
    replay_settings.llm.replay_path = script
    llm = build_llm(replay_settings, secrets)
    assert (
        isinstance(llm, ReplayLLM) and llm.chat([{"role": "user", "content": "x"}]).content == "hi"
    )
    real = Settings()
    real.llm = settings()
    client = build_llm(real, secrets)
    assert isinstance(client, OpenAICompatibleClient) and client.model_name == "chat-model"
    client.close()


# -- replay client ---------------------------------------------------------------------


def test_replay_matches_latest_input_and_sequences_responses() -> None:
    llm = ReplayLLM(
        entries=[
            ReplayEntry(
                match="hello",
                responses=[
                    response_from_dict({"content": "a"}),
                    response_from_dict({"content": "b"}),
                ],
            ),
            ReplayEntry(match=None, responses=[response_from_dict({"json": {"k": 1}})]),
        ]
    )
    user = [{"role": "user", "content": "Hello there"}]
    assert llm.chat(user).content == "a"
    assert llm.chat(user).content == "b"
    assert llm.chat(user).content == "b"  # repeats the last response
    assert json.loads(llm.chat([{"role": "user", "content": "other"}]).content or "") == {"k": 1}
    tool = response_from_dict({"tool_calls": [{"name": "t", "arguments": {"a": 1}}]})
    assert tool.tool_calls[0].name == "t" and tool.finish_reason == "tool_calls"
    assert len(llm.calls) == 4


def test_replay_without_match_raises_and_entries_can_be_exhausted() -> None:
    llm = ReplayLLM(
        entries=[
            ReplayEntry(match="x", responses=[response_from_dict({"content": "a"})], loop=False)
        ]
    )
    with pytest.raises(LLMError, match="no entry"):
        llm.chat([{"role": "user", "content": "y"}])
    assert llm.chat([{"role": "user", "content": "x"}]).content == "a"
    with pytest.raises(LLMError, match="exhausted"):
        llm.chat([{"role": "user", "content": "x"}])
    with pytest.raises(LLMError, match="no responses"):
        ReplayEntry(match=None, responses=[]).next_response()


def test_hash_embedding_is_deterministic_and_normalised() -> None:
    a = hash_embedding("customer balance", 16)
    assert a == hash_embedding("customer balance", 16)
    assert sum(x * x for x in a) == pytest.approx(1.0)
    assert hash_embedding("", 8) == [0.0] * 8
    llm = ReplayLLM(embeddings=False)
    with pytest.raises(LLMError):
        llm.embed(["x"])
