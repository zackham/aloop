"""Tests for reasoning/thinking mode support.

Covers:
- `reasoning_content` deltas → THINKING_DELTA events
- THINKING_START / THINKING_END boundaries
- `thinking` and `reasoning_effort` kwargs plumbed to payload
- Mode config carries thinking knobs through to requests
- complete() consumes thinking deltas without surfacing
"""

from __future__ import annotations

from unittest.mock import patch, AsyncMock

import pytest

from aloop import ALoop
from aloop.types import EventType, InferenceEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _stream_with_thinking_then_text(*a, **kw):
    """Mock provider that emits reasoning_content first, then content."""
    yield {"type": "thinking", "text": "let me think... "}
    yield {"type": "thinking", "text": "ok done."}
    yield {"type": "text", "text": "answer is 42"}
    yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


async def _stream_thinking_only(*a, **kw):
    """Provider emits thinking but never closes with text or tool — boundary edge case."""
    yield {"type": "thinking", "text": "hmm"}
    yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def _make_payload_capturing_completion(captured: dict):
    """Patch httpx so we can inspect the payload sent to the provider.

    Returns an async function suitable for patching ALoop._stream_completion's
    httpx.AsyncClient.stream method.
    """

    async def _capture(messages, system_prompt, tools, response_format=None,
                      *, temperature=None, max_tokens=None,
                      thinking=None, reasoning_effort=None):
        captured["thinking"] = thinking
        captured["reasoning_effort"] = reasoning_effort
        yield {"type": "text", "text": "ok"}
        yield {"type": "usage", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    return _capture


# ---------------------------------------------------------------------------
# Stream-side: reasoning_content → THINKING events
# ---------------------------------------------------------------------------

async def test_thinking_deltas_emit_thinking_events():
    """The agent loop emits THINKING_START + THINKING_DELTA + THINKING_END."""
    backend = ALoop(model="deepseek-v4-pro", api_key="test-key", provider="deepseek")

    events: list[InferenceEvent] = []
    with patch.object(backend, "_stream_completion", side_effect=_stream_with_thinking_then_text):
        async for event in backend.stream("hi", persist_session=False):
            events.append(event)
            if event.type == EventType.LOOP_END:
                break

    types = [e.type for e in events]
    # Order: LOOP_START, TURN_START, THINKING_START, THINKING_DELTA, THINKING_DELTA, THINKING_END, TEXT_DELTA, ...
    assert EventType.THINKING_START in types
    assert EventType.THINKING_DELTA in types
    assert EventType.THINKING_END in types

    # THINKING_START must come before any THINKING_DELTA, and THINKING_END
    # must come before TEXT_DELTA (boundary closes when text starts).
    start_idx = types.index(EventType.THINKING_START)
    delta_idx = types.index(EventType.THINKING_DELTA)
    end_idx = types.index(EventType.THINKING_END)
    text_idx = types.index(EventType.TEXT_DELTA)
    assert start_idx < delta_idx < end_idx < text_idx

    # Thinking text concatenates correctly
    thinking_chunks = [e.data["text"] for e in events if e.type == EventType.THINKING_DELTA]
    assert "".join(thinking_chunks) == "let me think... ok done."


async def test_thinking_end_emitted_at_turn_close_when_no_text_follows():
    """Edge case: stream emits thinking and then ends — THINKING_END still fires."""
    backend = ALoop(model="deepseek-v4-pro", api_key="test-key", provider="deepseek")

    events: list[InferenceEvent] = []
    with patch.object(backend, "_stream_completion", side_effect=_stream_thinking_only):
        async for event in backend.stream("hi", persist_session=False):
            events.append(event)
            if event.type in (EventType.LOOP_END, EventType.ERROR):
                break

    types = [e.type for e in events]
    assert EventType.THINKING_START in types
    assert EventType.THINKING_END in types
    # THINKING_END comes before LOOP_END / TURN_END
    end_idx = types.index(EventType.THINKING_END)
    # Find TURN_END (always present at turn close)
    if EventType.TURN_END in types:
        assert end_idx < types.index(EventType.TURN_END)


async def test_complete_consumes_thinking_silently():
    """complete() returns only the final text; thinking deltas are not surfaced."""
    backend = ALoop(model="deepseek-v4-pro", api_key="test-key", provider="deepseek")
    with patch.object(backend, "_stream_completion", side_effect=_stream_with_thinking_then_text):
        result = await backend.complete("hi")

    # Final text must NOT include the reasoning content
    assert result.text == "answer is 42"
    assert "think" not in result.text


# ---------------------------------------------------------------------------
# Request-side: thinking + reasoning_effort kwargs reach _stream_completion
# ---------------------------------------------------------------------------

async def test_constructor_thinking_kwargs_propagate_to_stream():
    """ALoop(thinking=..., reasoning_effort=...) defaults reach _stream_completion."""
    captured: dict = {}
    backend = ALoop(
        model="deepseek-v4-pro",
        api_key="test-key",
        provider="deepseek",
        thinking="enabled",
        reasoning_effort="max",
    )
    with patch.object(backend, "_stream_completion",
                      side_effect=_make_payload_capturing_completion(captured)):
        async for _ in backend.stream("hi", persist_session=False):
            pass

    assert captured["thinking"] == "enabled"
    assert captured["reasoning_effort"] == "max"


async def test_complete_thinking_kwargs_override_constructor():
    """Per-call thinking on complete() wins over constructor default."""
    captured: dict = {}
    backend = ALoop(
        model="deepseek-v4-pro",
        api_key="test-key",
        provider="deepseek",
        thinking="enabled",
        reasoning_effort="high",
    )
    with patch.object(backend, "_stream_completion",
                      side_effect=_make_payload_capturing_completion(captured)):
        await backend.complete("hi", thinking="disabled", reasoning_effort="max")

    assert captured["thinking"] == "disabled"
    assert captured["reasoning_effort"] == "max"


async def test_stream_thinking_kwargs_override_constructor():
    """Per-call thinking on stream() wins over constructor default."""
    captured: dict = {}
    backend = ALoop(
        model="deepseek-v4-pro",
        api_key="test-key",
        provider="deepseek",
        thinking="enabled",
        reasoning_effort="high",
    )
    with patch.object(backend, "_stream_completion",
                      side_effect=_make_payload_capturing_completion(captured)):
        async for _ in backend.stream(
            "hi",
            thinking="disabled",
            reasoning_effort="max",
            persist_session=False,
        ):
            pass

    assert captured["thinking"] == "disabled"
    assert captured["reasoning_effort"] == "max"


# ---------------------------------------------------------------------------
# Payload shape: top-level fields when knobs are set
# ---------------------------------------------------------------------------

async def test_payload_contains_thinking_and_effort_when_set():
    """The HTTP payload to the provider includes thinking and reasoning_effort."""
    import httpx
    backend = ALoop(
        model="deepseek-v4-pro",
        api_key="test-key",
        provider="deepseek",
        thinking="enabled",
        reasoning_effort="max",
    )

    captured_payload: dict = {}

    class _MockResponse:
        status_code = 200

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"ok"}}]}'
            yield 'data: {"usage":{"prompt_tokens":1,"completion_tokens":1}}'
            yield "data: [DONE]"

        async def aread(self):
            return b""

    class _MockStream:
        def __init__(self, response):
            self._response = response

        async def __aenter__(self):
            return self._response

        async def __aexit__(self, *args):
            return False

    class _MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, *, json, headers):
            captured_payload.update(json)
            return _MockStream(_MockResponse())

    with patch.object(httpx, "AsyncClient", return_value=_MockClient()):
        async for _ in backend._stream_completion(
            messages=[{"role": "user", "content": "hi"}],
            system_prompt=None,
            tools=None,
            thinking="enabled",
            reasoning_effort="max",
        ):
            pass

    assert captured_payload.get("thinking") == {"type": "enabled"}
    assert captured_payload.get("reasoning_effort") == "max"


async def test_payload_omits_thinking_when_unset():
    """When thinking and reasoning_effort are None, payload doesn't include them."""
    import httpx
    backend = ALoop(model="deepseek-v4-pro", api_key="test-key", provider="deepseek")

    captured_payload: dict = {}

    class _MockResponse:
        status_code = 200

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"ok"}}]}'
            yield "data: [DONE]"

        async def aread(self):
            return b""

    class _MockStream:
        def __init__(self, response):
            self._response = response

        async def __aenter__(self):
            return self._response

        async def __aexit__(self, *args):
            return False

    class _MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, *, json, headers):
            captured_payload.update(json)
            return _MockStream(_MockResponse())

    with patch.object(httpx, "AsyncClient", return_value=_MockClient()):
        async for _ in backend._stream_completion(
            messages=[{"role": "user", "content": "hi"}],
            system_prompt=None,
            tools=None,
        ):
            pass

    assert "thinking" not in captured_payload
    assert "reasoning_effort" not in captured_payload


# ---------------------------------------------------------------------------
# reasoning_content delta parsing inside _stream_completion
# ---------------------------------------------------------------------------

async def test_reasoning_content_delta_yields_thinking_chunks():
    """A raw `reasoning_content` field in a delta becomes a `thinking` chunk."""
    import httpx
    backend = ALoop(model="deepseek-v4-pro", api_key="test-key", provider="deepseek")

    class _MockResponse:
        status_code = 200

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}'
            yield 'data: {"choices":[{"delta":{"content":"hi"}}]}'
            yield "data: [DONE]"

        async def aread(self):
            return b""

    class _MockStream:
        def __init__(self, response):
            self._response = response

        async def __aenter__(self):
            return self._response

        async def __aexit__(self, *args):
            return False

    class _MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, *, json, headers):
            return _MockStream(_MockResponse())

    chunks = []
    with patch.object(httpx, "AsyncClient", return_value=_MockClient()):
        async for chunk in backend._stream_completion(
            messages=[{"role": "user", "content": "hi"}],
            system_prompt=None,
            tools=None,
        ):
            chunks.append(chunk)

    types = [c.get("type") for c in chunks]
    assert "thinking" in types
    assert "text" in types
    # Order: thinking before text
    assert types.index("thinking") < types.index("text")
    thinking_chunk = next(c for c in chunks if c.get("type") == "thinking")
    assert thinking_chunk["text"] == "thinking..."


# ---------------------------------------------------------------------------
# Mode config carries thinking knobs
# ---------------------------------------------------------------------------

async def test_mode_thinking_propagates_to_request(tmp_path, monkeypatch):
    """A mode with thinking + reasoning_effort plumbs them into the payload."""
    config = tmp_path / ".aloop"
    config.mkdir()
    config_file = config / "config.json"
    config_file.write_text(
        '{"modes": {"reasoner": {"thinking": "enabled", "reasoning_effort": "max"}}}'
    )

    monkeypatch.setenv("ALOOP_PROJECT_ROOT", str(tmp_path))

    captured: dict = {}
    backend = ALoop(model="deepseek-v4-pro", api_key="test-key", provider="deepseek")
    with patch.object(backend, "_stream_completion",
                      side_effect=_make_payload_capturing_completion(captured)):
        async for _ in backend.stream("hi", mode="reasoner", persist_session=False):
            pass

    assert captured["thinking"] == "enabled"
    assert captured["reasoning_effort"] == "max"


async def test_per_call_kwargs_override_mode_thinking(tmp_path, monkeypatch):
    """Explicit per-call thinking wins over mode-config thinking."""
    config = tmp_path / ".aloop"
    config.mkdir()
    config_file = config / "config.json"
    config_file.write_text(
        '{"modes": {"reasoner": {"thinking": "enabled", "reasoning_effort": "max"}}}'
    )
    monkeypatch.setenv("ALOOP_PROJECT_ROOT", str(tmp_path))

    captured: dict = {}
    backend = ALoop(model="deepseek-v4-pro", api_key="test-key", provider="deepseek")
    with patch.object(backend, "_stream_completion",
                      side_effect=_make_payload_capturing_completion(captured)):
        async for _ in backend.stream(
            "hi",
            mode="reasoner",
            thinking="disabled",
            reasoning_effort="high",
            persist_session=False,
        ):
            pass

    assert captured["thinking"] == "disabled"
    assert captured["reasoning_effort"] == "high"
