"""Tests for ALoop.complete() — one-shot completion without agent loop."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from aloop import ALoop, InferenceError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _simple_stream(*a, **kw):
    yield {"type": "text", "text": "Hello "}
    yield {"type": "text", "text": "world"}
    yield {"type": "usage", "usage": {"prompt_tokens": 100, "completion_tokens": 50}}


def _make_capturing_stream(captured: dict):
    """Return an async generator that records args/kwargs into *captured*."""

    async def _stream(messages, system_prompt, tools, response_format=None,
                      *, temperature=None, max_tokens=None):
        captured.update({
            "messages": messages,
            "system_prompt": system_prompt,
            "tools": tools,
            "response_format": response_format,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        yield {"type": "text", "text": "ok"}
        yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    return _stream


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_basic_completion():
    backend = ALoop(model="minimax-m2.5", api_key="test-key")
    with patch.object(backend, "_stream_completion", side_effect=_simple_stream):
        result = await backend.complete("hi")

    assert result.text == "Hello world"
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.turns == 1
    assert result.model == backend.model_config.id


async def test_system_prompt_none():
    captured: dict = {}
    backend = ALoop(model="minimax-m2.5", api_key="test-key")
    with patch.object(backend, "_stream_completion", side_effect=_make_capturing_stream(captured)):
        await backend.complete("hi")

    assert captured["system_prompt"] is None


async def test_system_prompt_empty_string():
    captured: dict = {}
    backend = ALoop(model="minimax-m2.5", api_key="test-key")
    with patch.object(backend, "_stream_completion", side_effect=_make_capturing_stream(captured)):
        await backend.complete("hi", system_prompt="")

    assert captured["system_prompt"] is None


async def test_system_prompt_provided():
    captured: dict = {}
    backend = ALoop(model="minimax-m2.5", api_key="test-key")
    with patch.object(backend, "_stream_completion", side_effect=_make_capturing_stream(captured)):
        await backend.complete("hi", system_prompt="You are helpful")

    assert captured["system_prompt"] == "You are helpful"


async def test_temperature_none():
    captured: dict = {}
    backend = ALoop(model="minimax-m2.5", api_key="test-key")
    with patch.object(backend, "_stream_completion", side_effect=_make_capturing_stream(captured)):
        await backend.complete("hi")

    assert captured["temperature"] is None


async def test_temperature_provided():
    captured: dict = {}
    backend = ALoop(model="minimax-m2.5", api_key="test-key")
    with patch.object(backend, "_stream_completion", side_effect=_make_capturing_stream(captured)):
        await backend.complete("hi", temperature=0.3)

    assert captured["temperature"] == 0.3


async def test_max_tokens_none():
    captured: dict = {}
    backend = ALoop(model="minimax-m2.5", api_key="test-key")
    with patch.object(backend, "_stream_completion", side_effect=_make_capturing_stream(captured)):
        await backend.complete("hi")

    assert captured["max_tokens"] is None


async def test_max_tokens_provided():
    captured: dict = {}
    backend = ALoop(model="minimax-m2.5", api_key="test-key")
    with patch.object(backend, "_stream_completion", side_effect=_make_capturing_stream(captured)):
        await backend.complete("hi", max_tokens=500)

    assert captured["max_tokens"] == 500


async def test_response_format():
    captured: dict = {}
    backend = ALoop(model="minimax-m2.5", api_key="test-key")
    with patch.object(backend, "_stream_completion", side_effect=_make_capturing_stream(captured)):
        await backend.complete("hi", response_format={"type": "json_object"})

    assert captured["response_format"] == {"type": "json_object"}


async def test_error_event():
    async def _error_stream(*a, **kw):
        yield {"type": "error", "message": "rate limited"}

    backend = ALoop(model="minimax-m2.5", api_key="test-key")
    with patch.object(backend, "_stream_completion", side_effect=_error_stream):
        with pytest.raises(InferenceError, match="rate limited"):
            await backend.complete("hi")


async def test_cost_calculation():
    async def _usage_stream(*a, **kw):
        yield {"type": "text", "text": "ok"}
        yield {"type": "usage", "usage": {"prompt_tokens": 1000, "completion_tokens": 500}}

    backend = ALoop(model="minimax-m2.5", api_key="test-key")
    # Override cost values for deterministic test (frozen dataclass)
    object.__setattr__(backend.model_config, "cost_input", 1.0)
    object.__setattr__(backend.model_config, "cost_output", 3.0)

    with patch.object(backend, "_stream_completion", side_effect=_usage_stream):
        result = await backend.complete("hi")

    # (1000/1M)*1.0 + (500/1M)*3.0 = 0.001 + 0.0015 = 0.0025
    assert result.cost_usd == pytest.approx(0.0025)


async def test_no_tools_attached():
    captured: dict = {}
    backend = ALoop(model="minimax-m2.5", api_key="test-key")
    with patch.object(backend, "_stream_completion", side_effect=_make_capturing_stream(captured)):
        await backend.complete("hi")

    assert captured["tools"] is None


async def test_no_api_key():
    backend = ALoop(model="minimax-m2.5", api_key="test-key")
    backend.api_key = ""
    with pytest.raises(InferenceError):
        await backend.complete("hi")


async def test_passthrough_to_stream_completion():
    captured: dict = {}
    backend = ALoop(model="minimax-m2.5", api_key="test-key")
    with patch.object(backend, "_stream_completion", side_effect=_make_capturing_stream(captured)):
        await backend.complete(
            "hi",
            system_prompt="be nice",
            temperature=0.7,
            max_tokens=200,
            response_format={"type": "json_object"},
        )

    assert captured["system_prompt"] == "be nice"
    assert captured["temperature"] == 0.7
    assert captured["max_tokens"] == 200
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["tools"] is None
    assert captured["messages"] == [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------------------
# Multi-provider passthrough — confirms provider resolution flows through
# complete() for every built-in tested provider. Mock-only; no network.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider_name", ["openrouter", "openai", "anthropic", "google", "groq"])
async def test_complete_works_with_provider(provider_name):
    backend = ALoop(
        model="some-model-id",
        provider=provider_name,
        api_key="test-key",
    )
    assert backend.provider.name is not None
    # Provider name on ProviderConfig is the human label; the registry key
    # lookup is what we care about — confirm get_provider(name) resolved.
    from aloop.providers import get_provider
    assert backend.provider is get_provider(provider_name)

    with patch.object(backend, "_stream_completion", side_effect=_simple_stream):
        result = await backend.complete("hi")

    assert result.text == "Hello world"
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.turns == 1
    assert result.model == backend.model_config.id
