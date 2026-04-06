"""Tests for AgentExecutor protocol and InProcessExecutor."""

from __future__ import annotations

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from aloop.agent_result import AgentResult, FORK_BOILERPLATE
from aloop.executor import (
    AgentExecutor,
    AgentExecutionHandle,
    InProcessExecutor,
    _write_spawn_metadata,
)
from aloop.session import AgentSession
from aloop.types import EventType, InferenceError, InferenceEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_parent_loop(*, current_mode_name=None, current_session=None):
    """Build a fake parent ALoop with the attributes the executor reads."""
    p = MagicMock()
    p._current_mode_name = current_mode_name
    p._current_session = current_session
    p.api_key = "test-key"
    p._default_model_config = MagicMock(id="test-model")
    p._default_provider = MagicMock(name="test-provider")
    p._default_compaction = MagicMock()
    p._default_max_iterations = 50
    p.max_session_age = 14400.0
    p.max_session_messages = 100
    # Per-stream state attributes that the snapshot/restore touches
    p.model_config = p._default_model_config
    p.provider = p._default_provider
    p.compaction_settings = p._default_compaction
    p.max_iterations = 50
    p._active_permissions = None
    p._active_allowed_tools = None
    p._last_compaction = None
    p._input_tokens = 0
    p._output_tokens = 0
    p._last_usage = None
    p._last_usage_index = None
    return p


def _make_event(event_type: EventType, data: dict, session_id=None):
    return InferenceEvent(event_type, data, session_id=session_id)


def _make_async_iter(events):
    async def _gen():
        for e in events:
            yield e
    return _gen()


# ---------------------------------------------------------------------------
# Fork-path validation
# ---------------------------------------------------------------------------


class TestInProcessExecutorForkPath:
    @pytest.mark.asyncio
    async def test_fork_path_uses_fork_kwargs(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            captured: dict = {}
            events = [
                _make_event(
                    EventType.LOOP_START,
                    {"session_id": "child_xyz"},
                    session_id="child_xyz",
                ),
                _make_event(
                    EventType.LOOP_END,
                    {
                        "text": "child output",
                        "session_id": "child_xyz",
                        "input_tokens": 5,
                        "output_tokens": 3,
                        "turns": 1,
                    },
                    session_id="child_xyz",
                ),
            ]

            def stream_fn(**kwargs):
                captured.update(kwargs)
                return _make_async_iter(events)

            parent = _make_parent_loop(current_mode_name="orchestrator")
            parent.stream = stream_fn

            executor = InProcessExecutor()
            handle = await executor.spawn(
                prompt="do thing",
                mode=None,
                model=None,
                parent_session_id="parent1",
                parent_turn_id="t999",
                fork=True,
                parent_loop=parent,
            )
            result = await handle.result()

            assert captured["fork_from"] == "parent1"
            assert captured["fork_at"] == "t999"
            assert captured["prompt"].startswith(FORK_BOILERPLATE)
            assert "do thing" in captured["prompt"]
            assert "mode" not in captured  # fork path inherits parent

            assert result.text == "child output"
            assert result.spawn_kind == "fork"
            assert result.session_id == "child_xyz"

    @pytest.mark.asyncio
    async def test_fork_path_requires_parent_session(self):
        executor = InProcessExecutor()
        parent = _make_parent_loop()
        with pytest.raises(ValueError, match="parent_session_id"):
            await executor.spawn(
                prompt="x",
                mode=None,
                model=None,
                parent_session_id=None,
                parent_turn_id="t1",
                fork=True,
                parent_loop=parent,
            )

    @pytest.mark.asyncio
    async def test_fork_path_requires_parent_turn_id(self):
        executor = InProcessExecutor()
        parent = _make_parent_loop()
        with pytest.raises(ValueError, match="parent_session_id"):
            await executor.spawn(
                prompt="x",
                mode=None,
                model=None,
                parent_session_id="p1",
                parent_turn_id=None,
                fork=True,
                parent_loop=parent,
            )


# ---------------------------------------------------------------------------
# Fresh path
# ---------------------------------------------------------------------------


class TestInProcessExecutorFreshPath:
    @pytest.mark.asyncio
    async def test_fresh_path_builds_new_loop(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            captured: dict = {}
            events = [
                _make_event(
                    EventType.LOOP_START,
                    {"session_id": "fresh_child"},
                    session_id="fresh_child",
                ),
                _make_event(
                    EventType.LOOP_END,
                    {
                        "text": "done",
                        "session_id": "fresh_child",
                        "input_tokens": 1,
                        "output_tokens": 2,
                        "turns": 1,
                    },
                ),
            ]

            fresh_loop = MagicMock()

            def fresh_stream(**kwargs):
                captured.update(kwargs)
                return _make_async_iter(events)

            fresh_loop.stream = fresh_stream

            parent = _make_parent_loop()
            executor = InProcessExecutor()

            with patch.object(executor, "_build_fresh_loop", return_value=fresh_loop) as build_mock:
                handle = await executor.spawn(
                    prompt="please review",
                    mode="reviewer",
                    model=None,
                    parent_session_id="parent1",
                    parent_turn_id="t1",
                    fork=False,
                    parent_loop=parent,
                )
                result = await handle.result()
                build_mock.assert_called_once()

            assert captured["mode"] == "reviewer"
            assert captured["prompt"] == "please review"
            assert result.spawn_kind == "fresh"
            assert result.mode == "reviewer"

    @pytest.mark.asyncio
    async def test_fresh_path_passes_model_override(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            fresh_loop = MagicMock()

            def fresh_stream(**kwargs):
                async def _gen():
                    yield _make_event(
                        EventType.LOOP_END,
                        {"text": "ok", "session_id": "s1", "turns": 1},
                    )
                return _gen()

            fresh_loop.stream = fresh_stream

            parent = _make_parent_loop()
            executor = InProcessExecutor()

            with patch.object(executor, "_build_fresh_loop", return_value=fresh_loop) as build_mock:
                handle = await executor.spawn(
                    prompt="x",
                    mode="reviewer",
                    model="gpt-4o",
                    parent_session_id=None,
                    parent_turn_id=None,
                    fork=False,
                    parent_loop=parent,
                )
                await handle.result()

            kwargs = build_mock.call_args.kwargs
            assert kwargs["model"] == "gpt-4o"


# ---------------------------------------------------------------------------
# Handle / task semantics
# ---------------------------------------------------------------------------


class TestExecutorHandleSemantics:
    @pytest.mark.asyncio
    async def test_returns_handle_with_task(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            fresh_loop = MagicMock()
            fresh_loop.stream = lambda **kw: _make_async_iter([
                _make_event(EventType.LOOP_END, {"text": "done", "session_id": "s1", "turns": 1}),
            ])
            parent = _make_parent_loop()
            executor = InProcessExecutor()
            with patch.object(executor, "_build_fresh_loop", return_value=fresh_loop):
                handle = await executor.spawn(
                    prompt="x",
                    mode="m",
                    model=None,
                    parent_session_id=None,
                    parent_turn_id=None,
                    fork=False,
                    parent_loop=parent,
                )
                assert isinstance(handle, AgentExecutionHandle)
                assert handle._task is not None
                result = await handle.result()
                assert isinstance(result, AgentResult)

    @pytest.mark.asyncio
    async def test_collects_usage_from_loop_end(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            fresh_loop = MagicMock()
            fresh_loop.stream = lambda **kw: _make_async_iter([
                _make_event(
                    EventType.LOOP_END,
                    {
                        "text": "done",
                        "session_id": "s1",
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cost_usd": 0.001,
                        "model": "test",
                        "turns": 3,
                    },
                ),
            ])
            parent = _make_parent_loop()
            executor = InProcessExecutor()
            with patch.object(executor, "_build_fresh_loop", return_value=fresh_loop):
                handle = await executor.spawn(
                    prompt="x", mode="m", model=None,
                    parent_session_id=None, parent_turn_id=None,
                    fork=False, parent_loop=parent,
                )
                result = await handle.result()
                assert result.usage["input_tokens"] == 100
                assert result.usage["output_tokens"] == 50
                assert result.usage["turns"] == 3

    @pytest.mark.asyncio
    async def test_extracts_partial_result_on_max_iterations(self, tmp_path):
        # Simulate a child that runs out of iterations: no LOOP_END text,
        # but messages contain assistant text. extract_partial_result
        # should pull it from session.
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            child_session = AgentSession(
                session_id="exhausted",
                messages=[
                    {"role": "user", "content": "hi", "turn_id": "t1"},
                    {"role": "assistant", "content": "partial answer", "turn_id": "t1"},
                ],
            )
            child_session.save_context()

            fresh_loop = MagicMock()

            def fresh_stream(**kw):
                async def _gen():
                    yield _make_event(EventType.LOOP_START, {"session_id": "exhausted"})
                    yield _make_event(EventType.ERROR, {"message": "Max iterations (1) reached"})
                return _gen()

            fresh_loop.stream = fresh_stream

            parent = _make_parent_loop()
            executor = InProcessExecutor()
            with patch.object(executor, "_build_fresh_loop", return_value=fresh_loop):
                handle = await executor.spawn(
                    prompt="x", mode="m", model=None,
                    parent_session_id=None, parent_turn_id=None,
                    fork=False, parent_loop=parent,
                )
                with pytest.raises(InferenceError):
                    await handle.result()

    @pytest.mark.asyncio
    async def test_propagates_inference_error(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            fresh_loop = MagicMock()
            fresh_loop.stream = lambda **kw: _make_async_iter([
                _make_event(EventType.ERROR, {"message": "kaboom"}),
            ])
            parent = _make_parent_loop()
            executor = InProcessExecutor()
            with patch.object(executor, "_build_fresh_loop", return_value=fresh_loop):
                handle = await executor.spawn(
                    prompt="x", mode="m", model=None,
                    parent_session_id=None, parent_turn_id=None,
                    fork=False, parent_loop=parent,
                )
                with pytest.raises(InferenceError, match="kaboom"):
                    await handle.result()

    @pytest.mark.asyncio
    async def test_handle_cancel_cancels_task(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            fresh_loop = MagicMock()

            def fresh_stream(**kw):
                async def _gen():
                    await asyncio.sleep(10)
                    yield _make_event(EventType.LOOP_END, {})
                return _gen()

            fresh_loop.stream = fresh_stream
            parent = _make_parent_loop()
            executor = InProcessExecutor()
            with patch.object(executor, "_build_fresh_loop", return_value=fresh_loop):
                handle = await executor.spawn(
                    prompt="x", mode="m", model=None,
                    parent_session_id=None, parent_turn_id=None,
                    fork=False, parent_loop=parent,
                )
                cancelled = handle.cancel()
                assert cancelled is True


# ---------------------------------------------------------------------------
# Spawn metadata persistence
# ---------------------------------------------------------------------------


class TestSpawnMetadataPersistence:
    @pytest.mark.asyncio
    async def test_writes_spawn_metadata_fork(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            # Create a real child session that the executor will load + update
            child = AgentSession(session_id="fork_child")
            child.save_context()

            events = [
                _make_event(EventType.LOOP_START, {"session_id": "fork_child"}),
                _make_event(
                    EventType.LOOP_END,
                    {"text": "ok", "session_id": "fork_child", "turns": 1},
                ),
            ]

            def stream_fn(**kw):
                return _make_async_iter(events)

            parent = _make_parent_loop(current_mode_name="orchestrator")
            parent.stream = stream_fn

            executor = InProcessExecutor()
            handle = await executor.spawn(
                prompt="x", mode=None, model=None,
                parent_session_id="parent1",
                parent_turn_id="t9",
                fork=True,
                parent_loop=parent,
            )
            await handle.result()

            loaded = AgentSession.load("fork_child")
            assert loaded is not None
            assert loaded.spawn_metadata is not None
            assert loaded.spawn_metadata["kind"] == "fork"
            assert loaded.spawn_metadata["parent_session_id"] == "parent1"
            assert loaded.spawn_metadata["parent_turn_id"] == "t9"
            assert loaded.spawn_metadata["spawning_mode"] == "orchestrator"

    @pytest.mark.asyncio
    async def test_writes_spawn_metadata_fresh(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            child = AgentSession(session_id="fresh_child")
            child.save_context()

            fresh_loop = MagicMock()
            fresh_loop.stream = lambda **kw: _make_async_iter([
                _make_event(EventType.LOOP_START, {"session_id": "fresh_child"}),
                _make_event(EventType.LOOP_END, {"text": "ok", "session_id": "fresh_child", "turns": 1}),
            ])

            parent = _make_parent_loop(current_mode_name="orchestrator")
            executor = InProcessExecutor()
            with patch.object(executor, "_build_fresh_loop", return_value=fresh_loop):
                handle = await executor.spawn(
                    prompt="x", mode="reviewer", model=None,
                    parent_session_id=None, parent_turn_id=None,
                    fork=False, parent_loop=parent,
                )
                await handle.result()

            loaded = AgentSession.load("fresh_child")
            assert loaded is not None
            assert loaded.spawn_metadata is not None
            assert loaded.spawn_metadata["kind"] == "fresh"
            assert loaded.spawn_metadata["child_mode"] == "reviewer"
            assert loaded.spawn_metadata["spawning_mode"] == "orchestrator"

    @pytest.mark.asyncio
    async def test_handles_missing_session_id_gracefully(self, tmp_path):
        # If the child stream never emits LOOP_START, no session_id is
        # captured and metadata can't be persisted. Should not crash.
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            fresh_loop = MagicMock()
            fresh_loop.stream = lambda **kw: _make_async_iter([
                _make_event(EventType.LOOP_END, {"text": "ok", "turns": 1}),
            ])

            parent = _make_parent_loop()
            executor = InProcessExecutor()
            with patch.object(executor, "_build_fresh_loop", return_value=fresh_loop):
                handle = await executor.spawn(
                    prompt="x", mode="m", model=None,
                    parent_session_id=None, parent_turn_id=None,
                    fork=False, parent_loop=parent,
                )
                result = await handle.result()
                assert result.session_id == ""


# ---------------------------------------------------------------------------
# Protocol checks
# ---------------------------------------------------------------------------


class TestAgentExecutorProtocol:
    def test_protocol_runtime_checkable(self):
        assert isinstance(InProcessExecutor(), AgentExecutor)


# ---------------------------------------------------------------------------
# _write_spawn_metadata helper
# ---------------------------------------------------------------------------


class TestWriteSpawnMetadata:
    def test_write_persists_to_session(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            s = AgentSession(session_id="test_meta")
            s.save_context()
            _write_spawn_metadata(
                s,
                spawn_kind="fresh",
                parent_session_id="parent1",
                parent_turn_id="t1",
                spawning_mode="ortho",
                child_mode="reviewer",
            )

            loaded = AgentSession.load("test_meta")
            assert loaded.spawn_metadata is not None
            assert loaded.spawn_metadata["kind"] == "fresh"
            assert "timestamp" in loaded.spawn_metadata
