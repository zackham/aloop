"""Tests for agent loop hardening — per-model timeout, stale sessions, empty responses."""

import time
from unittest.mock import patch

import pytest

from aloop.models import DEFAULT_MODELS, ModelConfig, get_model
from aloop.session import AgentSession


# ---------------------------------------------------------------------------
# R1: Per-model configurable stream timeout
# ---------------------------------------------------------------------------


class TestStreamTimeout:
    def test_unknown_model_gets_defaults(self):
        """Unknown model IDs get a default ModelConfig with 60s timeout."""
        model = get_model("some-provider/some-model")
        assert model.stream_timeout == 60.0
        assert model.id == "some-provider/some-model"
        assert model.name == "some-provider/some-model"

    def test_explicit_timeout_on_custom_model(self):
        """ModelConfig with explicit stream_timeout preserves it."""
        model = ModelConfig(
            id="test/slow-model",
            name="Slow Model",
            stream_timeout=120.0,
        )
        assert model.stream_timeout == 120.0

    def test_custom_model_timeout(self):
        """Custom ModelConfig can set arbitrary stream_timeout."""
        model = ModelConfig(
            id="test/fast",
            name="Test Fast",
            context_window=8000,
            max_output=4000,
            cost_input=1.0,
            cost_output=2.0,
            stream_timeout=15.0,
        )
        assert model.stream_timeout == 15.0

    def test_default_timeout_when_omitted(self):
        """ModelConfig without explicit stream_timeout gets 60.0."""
        model = ModelConfig(
            id="test/default",
            name="Test",
            context_window=8000,
            max_output=4000,
            cost_input=1.0,
            cost_output=2.0,
        )
        assert model.stream_timeout == 60.0

    def test_stream_completion_uses_model_timeout(self):
        """Verify _stream_completion references model config, not hardcoded 300."""
        import inspect
        from aloop.agent_backend import AgentLoopBackend

        backend = AgentLoopBackend(model="minimax-m2.5", api_key="test")
        source = inspect.getsource(backend._stream_completion)
        assert "300.0" not in source, "hardcoded 300.0 timeout still present"
        assert "stream_timeout" in source, "stream_timeout not referenced"


# ---------------------------------------------------------------------------
# R2: Stale session auto-clear
# ---------------------------------------------------------------------------


class TestSessionStaleness:
    def test_fresh_session_not_stale(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            session = AgentSession(session_id="s1")
            session.messages = [{"role": "user", "content": "hi"}]
            session.last_active = time.time()
            assert not session.is_stale()

    def test_stale_by_age(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            session = AgentSession(session_id="s1")
            session.messages = [{"role": "user", "content": "hi"}]
            session.last_active = time.time() - 20000  # ~5.5 hours ago
            assert session.is_stale()

    def test_stale_by_message_count(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            session = AgentSession(session_id="s1")
            session.messages = [{"role": "user", "content": f"m{i}"} for i in range(101)]
            session.last_active = time.time()
            assert session.is_stale()

    def test_exactly_at_threshold_not_stale(self, tmp_path):
        """100 messages exactly = not stale (> not >=)."""
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            session = AgentSession(session_id="s1")
            session.messages = [{"role": "user", "content": f"m{i}"} for i in range(100)]
            session.last_active = time.time()
            assert not session.is_stale()

    def test_custom_thresholds(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            session = AgentSession(session_id="s1")
            session.messages = [{"role": "user", "content": "hi"}] * 5
            session.last_active = time.time() - 100
            assert not session.is_stale()  # defaults
            assert session.is_stale(max_age_seconds=50)  # age triggers
            assert session.is_stale(max_messages=3)  # count triggers

    def test_empty_session_not_stale(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            session = AgentSession(session_id="s1")
            session.messages = []
            session.last_active = time.time() - 99999
            # is_stale returns True (old), but _resolve_session guards with
            # `if session.messages` so this never triggers clearing
            assert session.is_stale()  # method itself says yes


class TestResolveSessionClearing:
    @staticmethod
    def _write_stale_session(tmp_path, session_id, messages, last_active):
        """Write a session context file with a specific last_active timestamp.

        save_context() always writes last_active=time.time(), so we write
        the JSON directly to simulate a session that went stale on disk.
        """
        import json
        tmp_path.mkdir(parents=True, exist_ok=True)
        ctx_path = tmp_path / f"{session_id}.context.json"
        ctx_path.write_text(json.dumps({
            "session_id": session_id,
            "messages": messages,
            "last_compaction": None,
            "created_at": last_active - 1000,
            "last_active": last_active,
        }), encoding="utf-8")

    def test_stale_session_cleared_on_resolve(self, tmp_path):
        """_resolve_session clears a stale loaded session."""
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.agent_backend import AgentLoopBackend

            sid = "test_fixed"
            messages = [{"role": "user", "content": f"m{i}"} for i in range(10)]
            self._write_stale_session(
                tmp_path, sid, messages, time.time() - 20000,
            )

            backend = AgentLoopBackend(model="minimax-m2.5", api_key="test")
            resolved = backend._resolve_session(kwargs={"session_key": sid})
            assert resolved.messages == [], "stale session should be cleared"

    def test_stale_session_log_event(self, tmp_path):
        """Clearing a stale session logs session_auto_cleared event."""
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.agent_backend import AgentLoopBackend

            sid = "test_fixed"
            messages = [{"role": "user", "content": "hi"}]
            self._write_stale_session(
                tmp_path, sid, messages, time.time() - 20000,
            )

            backend = AgentLoopBackend(model="minimax-m2.5", api_key="test")
            backend._resolve_session(kwargs={"session_key": sid})

            log_path = tmp_path / f"{sid}.log.jsonl"
            assert log_path.exists()
            import json
            entries = [json.loads(line) for line in log_path.read_text().strip().split("\n")]
            clear_events = [e for e in entries if e.get("event") == "session_auto_cleared"]
            assert len(clear_events) == 1
            assert "age_seconds" in clear_events[0]["data"]
            assert "message_count" in clear_events[0]["data"]

    def test_fresh_session_not_cleared(self, tmp_path):
        """_resolve_session keeps a fresh session intact."""
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.agent_backend import AgentLoopBackend

            sid = "test_fixed"
            fresh = AgentSession(session_id=sid)
            fresh.messages = [{"role": "user", "content": "hi"}]
            fresh.last_active = time.time()
            fresh.save_context()

            backend = AgentLoopBackend(model="minimax-m2.5", api_key="test")
            resolved = backend._resolve_session(kwargs={"session_key": sid})
            assert len(resolved.messages) == 1

    def test_new_session_not_cleared(self, tmp_path):
        """Brand-new session (no prior save) is not cleared."""
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.agent_backend import AgentLoopBackend

            backend = AgentLoopBackend(model="minimax-m2.5", api_key="test")
            resolved = backend._resolve_session(kwargs={"session_key": "new_session"})
            assert resolved.messages == []

    def test_custom_thresholds_via_init(self, tmp_path):
        """__init__ params propagate to staleness check."""
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.agent_backend import AgentLoopBackend

            sid = "test_fixed"
            messages = [{"role": "user", "content": "hi"}] * 10
            self._write_stale_session(
                tmp_path, sid, messages, time.time() - 60,
            )

            # Default thresholds: not stale (60s < 14400s, 10 < 100)
            b1 = AgentLoopBackend(model="minimax-m2.5", api_key="test")
            r1 = b1._resolve_session(kwargs={"session_key": sid})
            assert len(r1.messages) == 10

            # Tight thresholds: stale
            b2 = AgentLoopBackend(
                model="minimax-m2.5",
                api_key="test",
                max_session_age=30.0,
                max_session_messages=5,
            )
            r2 = b2._resolve_session(kwargs={"session_key": sid})
            assert r2.messages == []


# ---------------------------------------------------------------------------
# R3: Empty response handling
# ---------------------------------------------------------------------------


class TestEmptyResponse:
    @pytest.mark.asyncio
    async def test_empty_response_yields_complete(self):
        """Model returning no content + no tool_calls -> COMPLETE, not ERROR."""
        from aloop.agent_backend import AgentLoopBackend
        from aloop.types import EventType

        backend = AgentLoopBackend(model="minimax-m2.5", api_key="test-key")

        async def mock_stream(*args, **kwargs):
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 0}}

        with patch.object(backend, "_stream_completion", side_effect=mock_stream):
            events = []
            async for event in backend.stream("test prompt"):
                events.append(event)

        types = [e.type for e in events]
        assert EventType.COMPLETE in types, f"expected COMPLETE, got {types}"
        assert EventType.ERROR not in types, f"unexpected ERROR in {types}"

    @pytest.mark.asyncio
    async def test_empty_response_preserves_accumulated_text(self):
        """If prior turns had content, accumulated_text is preserved."""
        from aloop.agent_backend import AgentLoopBackend
        from aloop.types import EventType

        backend = AgentLoopBackend(model="minimax-m2.5", api_key="test-key")
        call_count = 0

        async def mock_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: return text + tool_call
                yield {"type": "text", "text": "thinking..."}
                yield {
                    "type": "tool_call_delta",
                    "index": 0,
                    "id": "tc1",
                    "function": {"name": "test_tool", "arguments": "{}"},
                }
                yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
            else:
                # Second call: empty response
                yield {"type": "usage", "usage": {"prompt_tokens": 20, "completion_tokens": 0}}

        from aloop.tools_base import ToolDef, ToolResult

        test_tool = ToolDef(
            name="test_tool",
            description="test",
            parameters={"type": "object", "properties": {}},
            execute=lambda: ToolResult(content="ok"),
        )

        with patch.object(backend, "_stream_completion", side_effect=mock_stream):
            events = []
            async for event in backend.stream("test", tools=[test_tool]):
                events.append(event)

        complete = next(e for e in events if e.type == EventType.COMPLETE)
        assert complete.data["text"] == "thinking..."

    @pytest.mark.asyncio
    async def test_empty_response_no_empty_assistant_in_messages(self, tmp_path):
        """Session messages must not contain empty assistant message."""
        from aloop.agent_backend import AgentLoopBackend
        from aloop.types import EventType

        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            backend = AgentLoopBackend(model="minimax-m2.5", api_key="test-key")

            async def mock_stream(*args, **kwargs):
                yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 0}}

            with patch.object(backend, "_stream_completion", side_effect=mock_stream):
                events = []
                async for event in backend.stream("test", session_key="test_empty"):
                    events.append(event)

            # Load the session and check messages
            session = AgentSession.load(session_id=f"test_empty")
            if session:
                for msg in session.messages:
                    if msg.get("role") == "assistant":
                        assert msg.get("content") != "", "empty assistant message found in session"

    @pytest.mark.asyncio
    async def test_empty_response_run_returns_result(self):
        """run() returns InferenceResult, not raises, on empty response."""
        from aloop.agent_backend import AgentLoopBackend
        from aloop.types import InferenceResult

        backend = AgentLoopBackend(model="minimax-m2.5", api_key="test-key")

        async def mock_stream(*args, **kwargs):
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 0}}

        with patch.object(backend, "_stream_completion", side_effect=mock_stream):
            result = await backend.run("test prompt")

        assert isinstance(result, InferenceResult)
        assert result.text == ""
