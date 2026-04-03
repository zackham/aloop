"""Tests for ACP server implementation."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aloop.acp import AloopAgent, _extract_text, _tool_kind
from aloop.types import EventType, InferenceEvent


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def agent():
    """Create an AloopAgent with a mock connection."""
    a = AloopAgent(model="minimax-m2.5")
    mock_conn = AsyncMock()
    a.on_connect(mock_conn)
    return a


@pytest.fixture
def mock_conn(agent):
    """Get the mock connection from the agent."""
    return agent._conn


# ── Initialize ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_initialize_returns_protocol_version(agent):
    from acp import PROTOCOL_VERSION

    resp = await agent.initialize(protocol_version=1)
    assert resp.protocol_version == PROTOCOL_VERSION


@pytest.mark.asyncio
async def test_initialize_returns_agent_info(agent):
    resp = await agent.initialize(protocol_version=1)
    assert resp.agent_info is not None
    assert resp.agent_info.name == "aloop"
    assert resp.agent_info.version == "0.1.0"


@pytest.mark.asyncio
async def test_initialize_returns_capabilities(agent):
    resp = await agent.initialize(protocol_version=1)
    caps = resp.agent_capabilities
    assert caps is not None
    assert caps.load_session is True


# ── New Session ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_new_session_returns_session_id(agent, tmp_path):
    resp = await agent.new_session(cwd=str(tmp_path))
    assert resp.session_id
    assert resp.session_id in agent._sessions


@pytest.mark.asyncio
async def test_new_session_sets_cwd(agent, tmp_path):
    import os

    resp = await agent.new_session(cwd=str(tmp_path))
    state = agent._sessions[resp.session_id]
    assert state.cwd == str(tmp_path)
    assert os.environ.get("ALOOP_PROJECT_ROOT") == str(tmp_path)


@pytest.mark.asyncio
async def test_new_session_creates_backend(agent, tmp_path):
    resp = await agent.new_session(cwd=str(tmp_path))
    state = agent._sessions[resp.session_id]
    assert state.backend is not None
    assert state.backend.model_config.id is not None


@pytest.mark.asyncio
async def test_multiple_sessions(agent, tmp_path):
    r1 = await agent.new_session(cwd=str(tmp_path))
    r2 = await agent.new_session(cwd=str(tmp_path))
    assert r1.session_id != r2.session_id
    assert len(agent._sessions) == 2


# ── Load Session ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_nonexistent_session_returns_none(agent, tmp_path):
    resp = await agent.load_session(cwd=str(tmp_path), session_id="nonexistent-id")
    assert resp is None


# ── Cancel ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_sets_event(agent, tmp_path):
    resp = await agent.new_session(cwd=str(tmp_path))
    state = agent._sessions[resp.session_id]
    assert not state.cancel_event.is_set()

    await agent.cancel(session_id=resp.session_id)
    assert state.cancel_event.is_set()


@pytest.mark.asyncio
async def test_cancel_unknown_session_is_noop(agent):
    # Should not raise
    await agent.cancel(session_id="nonexistent")


# ── Close Session ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_session_removes_state(agent, tmp_path):
    resp = await agent.new_session(cwd=str(tmp_path))
    sid = resp.session_id
    assert sid in agent._sessions

    await agent.close_session(session_id=sid)
    assert sid not in agent._sessions


# ── Prompt with mocked backend ────────────────────────────────────────


async def _fake_stream(events: list[InferenceEvent]):
    """Create a fake async iterator from a list of events."""
    for event in events:
        yield event


@pytest.mark.asyncio
async def test_prompt_text_delta(agent, mock_conn, tmp_path):
    """TEXT_DELTA events become agent_message_chunk notifications."""
    resp = await agent.new_session(cwd=str(tmp_path))
    sid = resp.session_id
    state = agent._sessions[sid]

    events = [
        InferenceEvent.text("Hello "),
        InferenceEvent.text("world"),
        InferenceEvent.complete(text="Hello world", cost_usd=0.001, usage={
            "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.001,
        }),
    ]

    with patch.object(state.backend, "stream", return_value=_fake_stream(events)):
        from acp.schema import TextContentBlock
        result = await agent.prompt(
            prompt=[TextContentBlock(type="text", text="Say hello")],
            session_id=sid,
        )

    assert result.stop_reason == "end_turn"

    # Check session_update calls: 2 text deltas + 1 usage update
    calls = mock_conn.session_update.call_args_list
    assert len(calls) == 3

    # First two are text chunks
    for i in range(2):
        call_kwargs = calls[i].kwargs
        assert call_kwargs["session_id"] == sid
        update = call_kwargs["update"]
        assert update.session_update == "agent_message_chunk"


@pytest.mark.asyncio
async def test_prompt_tool_start_and_end(agent, mock_conn, tmp_path):
    """TOOL_START/TOOL_END events become tool_call/tool_call_update notifications."""
    resp = await agent.new_session(cwd=str(tmp_path))
    sid = resp.session_id
    state = agent._sessions[sid]

    events = [
        InferenceEvent.tool_start("read_file", "tc_001", {"file_path": "/tmp/test.txt"}),
        InferenceEvent.tool_end("read_file", "tc_001", "file contents here", is_error=False),
        InferenceEvent.complete(text="", cost_usd=0.001, usage={
            "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.001,
        }),
    ]

    with patch.object(state.backend, "stream", return_value=_fake_stream(events)):
        from acp.schema import TextContentBlock
        result = await agent.prompt(
            prompt=[TextContentBlock(type="text", text="Read a file")],
            session_id=sid,
        )

    assert result.stop_reason == "end_turn"

    calls = mock_conn.session_update.call_args_list
    assert len(calls) == 3  # tool_call + tool_call_update + usage_update

    # Tool start
    tool_start_update = calls[0].kwargs["update"]
    assert tool_start_update.session_update == "tool_call"
    assert tool_start_update.tool_call_id == "tc_001"
    assert tool_start_update.title == "read_file"
    assert tool_start_update.kind == "read"
    assert tool_start_update.status == "in_progress"

    # Tool end
    tool_end_update = calls[1].kwargs["update"]
    assert tool_end_update.session_update == "tool_call_update"
    assert tool_end_update.tool_call_id == "tc_001"
    assert tool_end_update.status == "completed"


@pytest.mark.asyncio
async def test_prompt_tool_error(agent, mock_conn, tmp_path):
    """TOOL_END with is_error=True becomes tool_call_update with status=failed."""
    resp = await agent.new_session(cwd=str(tmp_path))
    sid = resp.session_id
    state = agent._sessions[sid]

    events = [
        InferenceEvent.tool_start("bash", "tc_002", {"command": "false"}),
        InferenceEvent.tool_end("bash", "tc_002", "command failed", is_error=True),
        InferenceEvent.complete(text="", cost_usd=0.0, usage={
            "input_tokens": 50, "output_tokens": 20, "cost_usd": 0.0,
        }),
    ]

    with patch.object(state.backend, "stream", return_value=_fake_stream(events)):
        from acp.schema import TextContentBlock
        result = await agent.prompt(
            prompt=[TextContentBlock(type="text", text="Run a command")],
            session_id=sid,
        )

    calls = mock_conn.session_update.call_args_list
    tool_end_update = calls[1].kwargs["update"]
    assert tool_end_update.status == "failed"


@pytest.mark.asyncio
async def test_prompt_thinking_delta(agent, mock_conn, tmp_path):
    """THINKING_DELTA events become agent_thought_chunk notifications."""
    resp = await agent.new_session(cwd=str(tmp_path))
    sid = resp.session_id
    state = agent._sessions[sid]

    events = [
        InferenceEvent(EventType.THINKING_DELTA, {"text": "Let me think..."}),
        InferenceEvent.text("Answer"),
        InferenceEvent.complete(text="Answer", cost_usd=0.0, usage={
            "input_tokens": 50, "output_tokens": 10, "cost_usd": 0.0,
        }),
    ]

    with patch.object(state.backend, "stream", return_value=_fake_stream(events)):
        from acp.schema import TextContentBlock
        result = await agent.prompt(
            prompt=[TextContentBlock(type="text", text="Think about this")],
            session_id=sid,
        )

    calls = mock_conn.session_update.call_args_list
    thought_update = calls[0].kwargs["update"]
    assert thought_update.session_update == "agent_thought_chunk"


@pytest.mark.asyncio
async def test_prompt_usage_update(agent, mock_conn, tmp_path):
    """COMPLETE event sends usage_update with cost and token counts."""
    resp = await agent.new_session(cwd=str(tmp_path))
    sid = resp.session_id
    state = agent._sessions[sid]

    events = [
        InferenceEvent.text("Done"),
        InferenceEvent.complete(text="Done", cost_usd=0.042, usage={
            "input_tokens": 1000, "output_tokens": 500, "cost_usd": 0.042,
        }),
    ]

    with patch.object(state.backend, "stream", return_value=_fake_stream(events)):
        from acp.schema import TextContentBlock
        result = await agent.prompt(
            prompt=[TextContentBlock(type="text", text="Do something")],
            session_id=sid,
        )

    calls = mock_conn.session_update.call_args_list
    # Last call should be usage_update
    usage_update = calls[-1].kwargs["update"]
    assert usage_update.session_update == "usage_update"
    assert usage_update.cost is not None
    assert usage_update.cost.amount == 0.042
    assert usage_update.cost.currency == "USD"


@pytest.mark.asyncio
async def test_prompt_error_event(agent, mock_conn, tmp_path):
    """ERROR event returns end_turn (error logged, not propagated as exception)."""
    resp = await agent.new_session(cwd=str(tmp_path))
    sid = resp.session_id
    state = agent._sessions[sid]

    events = [
        InferenceEvent.error("API rate limited"),
    ]

    with patch.object(state.backend, "stream", return_value=_fake_stream(events)):
        from acp.schema import TextContentBlock
        result = await agent.prompt(
            prompt=[TextContentBlock(type="text", text="Fail")],
            session_id=sid,
        )

    assert result.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_prompt_cancellation(agent, mock_conn, tmp_path):
    """Setting cancel event mid-stream returns cancelled stop_reason."""
    resp = await agent.new_session(cwd=str(tmp_path))
    sid = resp.session_id
    state = agent._sessions[sid]

    async def slow_stream(*args, **kwargs):
        yield InferenceEvent.text("Starting...")
        # Simulate cancel happening mid-stream
        state.cancel_event.set()
        yield InferenceEvent.text("This should not be sent")
        yield InferenceEvent.complete(text="", cost_usd=0.0, usage={
            "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.0,
        })

    with patch.object(state.backend, "stream", side_effect=slow_stream):
        from acp.schema import TextContentBlock
        result = await agent.prompt(
            prompt=[TextContentBlock(type="text", text="Cancel me")],
            session_id=sid,
        )

    assert result.stop_reason == "cancelled"


@pytest.mark.asyncio
async def test_prompt_unknown_session_raises(agent):
    """Prompting an unknown session raises ValueError."""
    from acp.schema import TextContentBlock
    with pytest.raises(ValueError, match="Unknown session"):
        await agent.prompt(
            prompt=[TextContentBlock(type="text", text="Hello")],
            session_id="nonexistent",
        )


@pytest.mark.asyncio
async def test_prompt_clears_cancel_event(agent, tmp_path):
    """Each prompt starts with cancel_event cleared."""
    resp = await agent.new_session(cwd=str(tmp_path))
    sid = resp.session_id
    state = agent._sessions[sid]

    # Set cancel from a previous operation
    state.cancel_event.set()

    events = [
        InferenceEvent.complete(text="", cost_usd=0.0, usage={
            "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.0,
        }),
    ]

    with patch.object(state.backend, "stream", return_value=_fake_stream(events)):
        from acp.schema import TextContentBlock
        result = await agent.prompt(
            prompt=[TextContentBlock(type="text", text="After cancel")],
            session_id=sid,
        )

    # Should succeed because cancel_event was cleared at start
    assert result.stop_reason == "end_turn"


# ── Event translation helpers ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_start_ignored(agent, mock_conn, tmp_path):
    """TURN_START events produce no ACP notification."""
    resp = await agent.new_session(cwd=str(tmp_path))
    sid = resp.session_id
    state = agent._sessions[sid]

    events = [
        InferenceEvent(EventType.TURN_START, {"iteration": 0}),
        InferenceEvent.complete(text="", cost_usd=0.0, usage={
            "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.0,
        }),
    ]

    with patch.object(state.backend, "stream", return_value=_fake_stream(events)):
        from acp.schema import TextContentBlock
        await agent.prompt(
            prompt=[TextContentBlock(type="text", text="Test")],
            session_id=sid,
        )

    # Only the usage_update from COMPLETE, no notification for TURN_START
    calls = mock_conn.session_update.call_args_list
    assert len(calls) == 1
    assert calls[0].kwargs["update"].session_update == "usage_update"


# ── Helper functions ───────────────────────────────────────────────────


def test_extract_text_from_text_blocks():
    """_extract_text handles TextContentBlock objects."""
    from acp.schema import TextContentBlock
    blocks = [
        TextContentBlock(type="text", text="Hello "),
        TextContentBlock(type="text", text="world"),
    ]
    assert _extract_text(blocks) == "Hello \nworld"


def test_extract_text_from_dicts():
    """_extract_text handles dict-style content blocks."""
    blocks = [
        {"type": "text", "text": "Hello"},
        {"type": "image", "url": "http://example.com/img.png"},
    ]
    assert _extract_text(blocks) == "Hello"


def test_extract_text_empty():
    assert _extract_text([]) == ""


def test_tool_kind_mapping():
    assert _tool_kind("read_file") == "read"
    assert _tool_kind("write_file") == "edit"
    assert _tool_kind("edit_file") == "edit"
    assert _tool_kind("bash") == "execute"
    assert _tool_kind("load_skill") == "other"
    assert _tool_kind("unknown_tool") == "other"


# ── Fork / Resume / Close ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fork_session(agent, tmp_path):
    resp = await agent.new_session(cwd=str(tmp_path))
    fork_resp = await agent.fork_session(cwd=str(tmp_path), session_id=resp.session_id)
    assert fork_resp.session_id != resp.session_id
    assert fork_resp.session_id in agent._sessions


@pytest.mark.asyncio
async def test_resume_nonexistent_creates_nothing(agent, tmp_path):
    resp = await agent.resume_session(cwd=str(tmp_path), session_id="nonexistent")
    # Returns a ResumeSessionResponse regardless (graceful)
    assert resp is not None


@pytest.mark.asyncio
async def test_list_sessions_empty(agent, tmp_path):
    resp = await agent.list_sessions(cwd=str(tmp_path))
    assert resp.sessions is not None


# ── PromptResponse includes usage ─────────────────────────────────────


@pytest.mark.asyncio
async def test_prompt_response_includes_usage(agent, mock_conn, tmp_path):
    """PromptResponse includes token usage."""
    resp = await agent.new_session(cwd=str(tmp_path))
    sid = resp.session_id
    state = agent._sessions[sid]

    # Set backend usage manually
    state.backend._input_tokens = 500
    state.backend._output_tokens = 200

    events = [
        InferenceEvent.complete(text="Done", cost_usd=0.01, usage={
            "input_tokens": 500, "output_tokens": 200, "cost_usd": 0.01,
        }),
    ]

    with patch.object(state.backend, "stream", return_value=_fake_stream(events)):
        from acp.schema import TextContentBlock
        result = await agent.prompt(
            prompt=[TextContentBlock(type="text", text="Test")],
            session_id=sid,
        )

    assert result.usage is not None
    assert result.usage.input_tokens == 500
    assert result.usage.output_tokens == 200
    assert result.usage.total_tokens == 700
