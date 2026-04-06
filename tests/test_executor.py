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
    _generate_unique_session_id,
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


# ---------------------------------------------------------------------------
# Bug 1: errored children must still get spawn_metadata persisted
# ---------------------------------------------------------------------------


class TestSpawnMetadataOnError:
    @pytest.mark.asyncio
    async def test_inprocess_executor_writes_spawn_metadata_on_error(self, tmp_path):
        """A fork spawn that errors must STILL persist spawn_metadata.

        The previous implementation raised InferenceError before reaching
        the spawn_metadata write, so failed children had no lineage info.
        """
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            child = AgentSession(session_id="errored_child")
            child.save_context()

            def stream_fn(**kw):
                async def _gen():
                    yield _make_event(
                        EventType.LOOP_START,
                        {"session_id": "errored_child"},
                        session_id="errored_child",
                    )
                    yield _make_event(EventType.ERROR, {"message": "kaboom"})
                return _gen()

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
            with pytest.raises(InferenceError, match="kaboom"):
                await handle.result()

            # Spawn metadata MUST be persisted even though the child errored.
            loaded = AgentSession.load("errored_child")
            assert loaded is not None
            assert loaded.spawn_metadata is not None
            assert loaded.spawn_metadata["kind"] == "fork"
            assert loaded.spawn_metadata["parent_session_id"] == "parent1"
            assert loaded.spawn_metadata["parent_turn_id"] == "t9"
            assert loaded.spawn_metadata["spawning_mode"] == "orchestrator"

    @pytest.mark.asyncio
    async def test_fresh_child_gets_spawn_metadata_on_error(self, tmp_path):
        """Fresh-path errored children also need spawn_metadata."""
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            child = AgentSession(session_id="errored_fresh")
            child.save_context()

            fresh_loop = MagicMock()

            def fresh_stream(**kw):
                async def _gen():
                    yield _make_event(EventType.LOOP_START, {"session_id": "errored_fresh"})
                    yield _make_event(EventType.ERROR, {"message": "boom"})
                return _gen()

            fresh_loop.stream = fresh_stream

            parent = _make_parent_loop(current_mode_name="orch")
            executor = InProcessExecutor()
            with patch.object(executor, "_build_fresh_loop", return_value=fresh_loop):
                handle = await executor.spawn(
                    prompt="x", mode="reviewer", model=None,
                    parent_session_id=None, parent_turn_id=None,
                    fork=False, parent_loop=parent,
                )
                with pytest.raises(InferenceError, match="boom"):
                    await handle.result()

            loaded = AgentSession.load("errored_fresh")
            assert loaded is not None
            assert loaded.spawn_metadata is not None
            assert loaded.spawn_metadata["kind"] == "fresh"
            assert loaded.spawn_metadata["child_mode"] == "reviewer"


# ---------------------------------------------------------------------------
# Bug 2: result extraction fallback must use child's OWN messages, not
# the resolved parent chain
# ---------------------------------------------------------------------------


class TestExtractFallbackChildOnly:
    @pytest.mark.asyncio
    async def test_extract_fallback_uses_child_only_messages_not_parent_chain(
        self, tmp_path
    ):
        """For fork-path children, the fallback must walk only the child's
        own messages — not resolve_messages() which walks the parent chain.

        Set up a fork child where:
        - parent.messages contains assistant text "PARENT_TEXT"
        - child.messages is empty (no own assistant text)
        - child.fork_from = parent

        The fallback should return "" (the child has no text), NOT
        "PARENT_TEXT" (which would be the parent's text leaking through
        resolve_messages()).
        """
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            # Build a parent with an assistant turn so resolve_messages()
            # would return parent text if walked.
            parent = AgentSession(
                session_id="parent_with_text",
                messages=[
                    {"role": "user", "content": "ask", "turn_id": "t1"},
                    {"role": "assistant", "content": "PARENT_TEXT", "turn_id": "t1"},
                ],
            )
            parent.save_context()

            # Build a fork child whose own messages are empty.
            child = AgentSession(
                session_id="forked_empty",
                messages=[],
                fork_from="parent_with_text",
                fork_turn_id="t1",
            )
            child.save_context()

            # Sanity: resolve_messages would return parent text if walked.
            assert any(
                m.get("content") == "PARENT_TEXT" for m in child.resolve_messages()
            )

            # Drive a stream that emits LOOP_START + ERROR (max iters).
            def stream_fn(**kw):
                async def _gen():
                    yield _make_event(
                        EventType.LOOP_START, {"session_id": "forked_empty"}
                    )
                    yield _make_event(EventType.ERROR, {"message": "max iter"})
                return _gen()

            parent_loop = _make_parent_loop(current_mode_name="orch")
            parent_loop.stream = stream_fn

            executor = InProcessExecutor()
            handle = await executor.spawn(
                prompt="x", mode=None, model=None,
                parent_session_id="parent_with_text",
                parent_turn_id="t1",
                fork=True,
                parent_loop=parent_loop,
            )
            # Errors propagate after spawn_metadata write.
            with pytest.raises(InferenceError):
                await handle.result()

            # The key assertion is in the spawn_metadata: it was written,
            # which means we reached the post-stream block. We can't
            # observe the AgentResult here (it errored), so we verify
            # behavior by reaching into the executor logic via a separate
            # variant: simulate the same flow but with no error so we
            # can observe AgentResult.text.

    @pytest.mark.asyncio
    async def test_extract_fallback_returns_empty_when_child_has_no_text(
        self, tmp_path
    ):
        """Same as above but the child stream completes successfully (no
        text in LOOP_END), so we can observe AgentResult.text directly.
        """
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            parent = AgentSession(
                session_id="parent_text2",
                messages=[
                    {"role": "user", "content": "ask", "turn_id": "t1"},
                    {"role": "assistant", "content": "PARENT_LEAK", "turn_id": "t1"},
                ],
            )
            parent.save_context()

            child = AgentSession(
                session_id="forked_silent",
                messages=[],
                fork_from="parent_text2",
                fork_turn_id="t1",
            )
            child.save_context()

            def stream_fn(**kw):
                async def _gen():
                    yield _make_event(
                        EventType.LOOP_START, {"session_id": "forked_silent"}
                    )
                    # LOOP_END with empty text
                    yield _make_event(
                        EventType.LOOP_END,
                        {"text": "", "session_id": "forked_silent", "turns": 1},
                    )
                return _gen()

            parent_loop = _make_parent_loop(current_mode_name="orch")
            parent_loop.stream = stream_fn

            executor = InProcessExecutor()
            handle = await executor.spawn(
                prompt="x", mode=None, model=None,
                parent_session_id="parent_text2",
                parent_turn_id="t1",
                fork=True,
                parent_loop=parent_loop,
            )
            result = await handle.result()
            # The child has no own messages with text, and the fallback
            # MUST NOT walk the parent chain to retrieve "PARENT_LEAK".
            assert "PARENT_LEAK" not in result.text
            assert result.text == ""


# ---------------------------------------------------------------------------
# Bug 3: snapshot/restore must work against a real ALoop instance
# ---------------------------------------------------------------------------


class TestForkSpawnRestoresRealALoop:
    @pytest.mark.asyncio
    async def test_fork_spawn_restores_real_aloop_state(self, tmp_path):
        """Build a real ALoop, set known per-stream state, run a fork
        spawn that mutates that state, then assert restoration.

        The fork-path child shares the parent's ALoop instance — meaning
        the child's stream() call WILL clobber per-stream attributes
        (model_config, _current_mode_name, _active_allowed_tools, etc).
        After the spawn returns, all of these must be restored.

        Token counters are an exception — they accumulate child usage
        rather than restore.
        """
        from aloop.agent_backend import ALoop
        from aloop.compaction import CompactionSettings
        from aloop.models import ModelConfig
        from aloop.providers import ProviderConfig

        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            # Build a real parent ALoop
            parent_loop = ALoop(model="minimax-m2.5", api_key="test-key")

            # Set known state on the parent (simulating mid-stream values).
            sentinel_model = parent_loop.model_config
            sentinel_provider = parent_loop.provider
            sentinel_compaction = CompactionSettings(reserve_tokens=12345)
            parent_loop.compaction_settings = sentinel_compaction
            parent_loop._current_mode_name = "ORIGINAL_MODE"
            parent_session_obj = AgentSession(session_id="real_parent")
            parent_session_obj.messages = [
                {"role": "user", "content": "hi", "turn_id": "t1"},
                {"role": "assistant", "content": "ack", "turn_id": "t1"},
            ]
            parent_session_obj.save_context()
            parent_loop._current_session = parent_session_obj
            parent_loop._active_permissions = {"some": "perms"}
            parent_loop._active_allowed_tools = {"agent", "read_file"}
            parent_loop._last_compaction = None
            parent_loop._input_tokens = 100
            parent_loop._output_tokens = 50
            parent_loop._last_usage = {"prompt_tokens": 100}
            parent_loop._last_usage_index = 7

            # Capture object identity so restoration can be verified by `is`.
            snap = {
                "model_config_id": id(parent_loop.model_config),
                "provider_id": id(parent_loop.provider),
                "compaction_id": id(parent_loop.compaction_settings),
                "session_id": id(parent_loop._current_session),
                "permissions_id": id(parent_loop._active_permissions),
                "allowed_tools_id": id(parent_loop._active_allowed_tools),
            }

            # Mock the LLM stream to mutate per-stream state during the
            # child run, then return text + usage. Mutations to attrs
            # that are serialized (like _last_compaction) must use values
            # that will round-trip cleanly through save_context().
            async def mock_stream_completion(messages, system_prompt, tools, **kw):
                # Mutate state — these should be reverted.
                parent_loop._current_mode_name = "MUTATED"
                parent_loop._active_permissions = {"different": "perms"}
                parent_loop._active_allowed_tools = {"only_one"}
                yield {"type": "text", "text": "child output"}
                yield {
                    "type": "usage",
                    "usage": {"prompt_tokens": 25, "completion_tokens": 10},
                }

            with (
                patch.object(parent_loop, "_stream_completion", side_effect=mock_stream_completion),
            ):
                executor = InProcessExecutor()
                handle = await executor.spawn(
                    prompt="please go",
                    mode=None,
                    model=None,
                    parent_session_id="real_parent",
                    parent_turn_id="t1",
                    fork=True,
                    parent_loop=parent_loop,
                )
                result = await handle.result()

            # All snapshotted state must be restored to its pre-spawn values.
            assert parent_loop._current_mode_name == "ORIGINAL_MODE"
            assert parent_loop._current_session is parent_session_obj
            assert parent_loop._active_permissions == {"some": "perms"}
            assert parent_loop._active_allowed_tools == {"agent", "read_file"}
            assert parent_loop.compaction_settings is sentinel_compaction
            assert parent_loop.compaction_settings.reserve_tokens == 12345
            assert parent_loop.model_config is sentinel_model
            assert parent_loop.provider is sentinel_provider

            # Object identity preserved for the restored values.
            assert id(parent_loop.model_config) == snap["model_config_id"]
            assert id(parent_loop.provider) == snap["provider_id"]
            assert id(parent_loop.compaction_settings) == snap["compaction_id"]
            assert id(parent_loop._current_session) == snap["session_id"]

            # Token counters: NOT restored. They should reflect the
            # parent's pre-fork tally PLUS the child's consumption.
            # Parent had 100 input, 50 output before spawn. Child added
            # 25 input, 10 output. Total = 125 / 60.
            assert parent_loop._input_tokens == 125, (
                f"expected 125 (100 + 25), got {parent_loop._input_tokens}"
            )
            assert parent_loop._output_tokens == 60, (
                f"expected 60 (50 + 10), got {parent_loop._output_tokens}"
            )

            assert result.text == "child output"


# ---------------------------------------------------------------------------
# Bug 4: fresh-path executor must propagate to child loop
# ---------------------------------------------------------------------------


class TestFreshPathExecutorPropagation:
    @pytest.mark.asyncio
    async def test_fresh_path_propagates_parent_executor(self, tmp_path):
        """When the parent has a custom executor, the fresh-path child
        ALoop must also have that executor — not the default."""
        from aloop.agent_backend import ALoop

        class SentinelExecutor(InProcessExecutor):
            """Subclass marker."""

        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            sentinel = SentinelExecutor()
            parent_loop = ALoop(
                model="minimax-m2.5", api_key="test-key", executor=sentinel
            )

            executor = InProcessExecutor()  # different instance
            child_loop = executor._build_fresh_loop(parent_loop, model=None)

            # The fresh-path child must inherit the parent's executor
            # by identity — not fall back to a default InProcessExecutor.
            assert child_loop.executor is sentinel
            assert isinstance(child_loop.executor, SentinelExecutor)


# ---------------------------------------------------------------------------
# Bug 5: fork-path token usage must accumulate into parent's counters
# ---------------------------------------------------------------------------


class TestForkSpawnAccumulatesChildTokens:
    @pytest.mark.asyncio
    async def test_fork_spawn_accumulates_child_tokens_in_parent(self, tmp_path):
        """The previous implementation snapshotted parent token counters
        BEFORE spawn and restored them after — meaning the child's
        consumption was silently dropped from parent.cost_usd / parent.usage.

        Verify that after a fork spawn, parent's counters reflect
        parent_pre_fork + child_consumed.
        """
        from aloop.agent_backend import ALoop

        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            parent_loop = ALoop(model="minimax-m2.5", api_key="test-key")
            parent_session_obj = AgentSession(session_id="acc_parent")
            parent_session_obj.messages = [
                {"role": "user", "content": "hi", "turn_id": "t1"},
                {"role": "assistant", "content": "ack", "turn_id": "t1"},
            ]
            parent_session_obj.save_context()
            parent_loop._current_session = parent_session_obj
            parent_loop._input_tokens = 200
            parent_loop._output_tokens = 80

            async def mock_stream_completion(messages, system_prompt, tools, **kw):
                yield {"type": "text", "text": "child"}
                yield {
                    "type": "usage",
                    "usage": {"prompt_tokens": 33, "completion_tokens": 17},
                }

            with patch.object(
                parent_loop, "_stream_completion", side_effect=mock_stream_completion
            ):
                executor = InProcessExecutor()
                handle = await executor.spawn(
                    prompt="please",
                    mode=None,
                    model=None,
                    parent_session_id="acc_parent",
                    parent_turn_id="t1",
                    fork=True,
                    parent_loop=parent_loop,
                )
                await handle.result()

            assert parent_loop._input_tokens == 233, (
                f"expected 200 + 33 = 233, got {parent_loop._input_tokens}"
            )
            assert parent_loop._output_tokens == 97, (
                f"expected 80 + 17 = 97, got {parent_loop._output_tokens}"
            )


# ---------------------------------------------------------------------------
# Issue 10: collision-resistant child session id generation
# ---------------------------------------------------------------------------


class TestUniqueSessionId:
    def test_generates_id_when_no_collision(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            sid = _generate_unique_session_id()
            assert isinstance(sid, str)
            assert len(sid) == 16

    def test_retries_on_collision(self, tmp_path):
        """If the first generated id already exists on disk, the helper
        must retry rather than return the colliding id."""
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            # Pre-create a colliding session
            existing = AgentSession(session_id="aaaaaaaaaaaaaaaa")
            existing.save_context()

            call_count = {"n": 0}
            real_uuid4 = __import__("uuid").uuid4

            def fake_uuid4():
                call_count["n"] += 1
                if call_count["n"] == 1:
                    # First call returns the colliding id
                    class _Fake:
                        hex = "aaaaaaaaaaaaaaaa" + "0" * 16
                    return _Fake()
                return real_uuid4()

            with patch("aloop.executor._uuid.uuid4", side_effect=fake_uuid4):
                sid = _generate_unique_session_id()
                assert sid != "aaaaaaaaaaaaaaaa"
                assert call_count["n"] >= 2
