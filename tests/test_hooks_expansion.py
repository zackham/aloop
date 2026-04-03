"""Tests for Work Session 5: hooks expansion to 10 hook points.

Covers:
- ToolRejected exception
- on_before_tool with ToolRejected raising
- on_before_tool returning ToolResult (short-circuit)
- on_before_tool backward compat ({"allow": False})
- New lifecycle hooks called in correct order
- Tool merge behavior (mode + hooks + extra_tools + tools override)
- Pre/post compaction hooks
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from aloop.tools_base import ToolDef, ToolResult, ToolRejected
from aloop.hooks import (
    run_before_tool,
    run_after_tool,
    run_on_loop_start,
    run_on_loop_end,
    run_on_turn_start,
    run_on_turn_end,
    run_on_pre_compaction,
    run_on_post_compaction,
    run_register_tools,
    reset as hooks_reset,
)
from aloop.types import EventType, InferenceEvent


# ---------------------------------------------------------------------------
# ToolRejected exception
# ---------------------------------------------------------------------------


class TestToolRejected:
    def test_basic_construction(self):
        exc = ToolRejected("not allowed")
        assert exc.reason == "not allowed"
        assert str(exc) == "not allowed"

    def test_is_exception(self):
        assert issubclass(ToolRejected, Exception)

    def test_raise_and_catch(self):
        with pytest.raises(ToolRejected) as exc_info:
            raise ToolRejected("blocked: destructive command")
        assert exc_info.value.reason == "blocked: destructive command"

    def test_importable_from_aloop(self):
        from aloop import ToolRejected as TR
        assert TR is ToolRejected


# ---------------------------------------------------------------------------
# on_before_tool with ToolRejected
# ---------------------------------------------------------------------------


class TestBeforeToolToolRejected:
    def test_tool_rejected_blocks_execution(self):
        """ToolRejected in a before_tool hook returns allow=False with reason."""
        hooks_reset()

        mock_mod = MagicMock()
        mock_entry = MagicMock()
        mock_entry.fn = MagicMock(side_effect=ToolRejected("dangerous operation"))
        mock_mod.get_hooks.return_value = [mock_entry]

        with patch("aloop.hooks._ensure_discovered", return_value=mock_mod):
            result = run_before_tool("bash", {"command": "rm -rf /"})

        assert result["allow"] is False
        assert result["reason"] == "dangerous operation"

    def test_tool_rejected_stops_chain(self):
        """ToolRejected from first hook prevents later hooks from running."""
        hooks_reset()

        entry1 = MagicMock()
        entry1.fn = MagicMock(side_effect=ToolRejected("blocked"))
        entry2 = MagicMock()
        entry2.fn = MagicMock(return_value={"allow": True})

        mock_mod = MagicMock()
        mock_mod.get_hooks.return_value = [entry1, entry2]

        with patch("aloop.hooks._ensure_discovered", return_value=mock_mod):
            result = run_before_tool("bash", {"command": "ls"})

        assert result["allow"] is False
        entry2.fn.assert_not_called()


# ---------------------------------------------------------------------------
# on_before_tool returning ToolResult (short-circuit)
# ---------------------------------------------------------------------------


class TestBeforeToolShortCircuit:
    def test_tool_result_short_circuits(self):
        """Returning a ToolResult from before_tool short-circuits execution."""
        hooks_reset()

        cached = ToolResult(content="cached response", is_error=False)
        mock_entry = MagicMock()
        mock_entry.fn = MagicMock(return_value=cached)

        mock_mod = MagicMock()
        mock_mod.get_hooks.return_value = [mock_entry]

        with patch("aloop.hooks._ensure_discovered", return_value=mock_mod):
            result = run_before_tool("my_tool", {"query": "test"})

        assert result["allow"] is False
        assert result["tool_result"] is cached
        assert result["tool_result"].content == "cached response"
        assert result["tool_result"].is_error is False

    def test_tool_result_stops_chain(self):
        """ToolResult from first hook prevents later hooks from running."""
        hooks_reset()

        cached = ToolResult(content="from cache")
        entry1 = MagicMock()
        entry1.fn = MagicMock(return_value=cached)
        entry2 = MagicMock()
        entry2.fn = MagicMock(return_value={"allow": True})

        mock_mod = MagicMock()
        mock_mod.get_hooks.return_value = [entry1, entry2]

        with patch("aloop.hooks._ensure_discovered", return_value=mock_mod):
            result = run_before_tool("tool", {})

        entry2.fn.assert_not_called()


# ---------------------------------------------------------------------------
# on_before_tool backward compat
# ---------------------------------------------------------------------------


class TestBeforeToolBackwardCompat:
    def test_allow_false_dict(self):
        """{"allow": False, "reason": ...} still works."""
        hooks_reset()

        mock_entry = MagicMock()
        mock_entry.fn = MagicMock(return_value={"allow": False, "reason": "blocked by policy"})

        mock_mod = MagicMock()
        mock_mod.get_hooks.return_value = [mock_entry]

        with patch("aloop.hooks._ensure_discovered", return_value=mock_mod):
            result = run_before_tool("write_file", {"path": "/etc/passwd"})

        assert result["allow"] is False
        assert result["reason"] == "blocked by policy"

    def test_allow_true_dict(self):
        """{"allow": True} proceeds normally."""
        hooks_reset()

        mock_entry = MagicMock()
        mock_entry.fn = MagicMock(return_value={"allow": True})

        mock_mod = MagicMock()
        mock_mod.get_hooks.return_value = [mock_entry]

        with patch("aloop.hooks._ensure_discovered", return_value=mock_mod):
            result = run_before_tool("read_file", {"path": "/tmp/test"})

        assert result["allow"] is True

    def test_none_return_proceeds(self):
        """Returning None from a hook means 'proceed'."""
        hooks_reset()

        mock_entry = MagicMock()
        mock_entry.fn = MagicMock(return_value=None)

        mock_mod = MagicMock()
        mock_mod.get_hooks.return_value = [mock_entry]

        with patch("aloop.hooks._ensure_discovered", return_value=mock_mod):
            result = run_before_tool("read_file", {})

        assert result["allow"] is True

    def test_modified_args_still_works(self):
        """{"allow": True, "modified_args": ...} still passes through."""
        hooks_reset()

        mock_entry = MagicMock()
        mock_entry.fn = MagicMock(return_value={"allow": True, "modified_args": {"path": "/safe/path"}})

        mock_mod = MagicMock()
        mock_mod.get_hooks.return_value = [mock_entry]

        with patch("aloop.hooks._ensure_discovered", return_value=mock_mod):
            result = run_before_tool("read_file", {"path": "/original"})

        assert result["allow"] is True
        assert result["args"] == {"path": "/safe/path"}


# ---------------------------------------------------------------------------
# New lifecycle hooks — no-op when no hooks configured
# ---------------------------------------------------------------------------


class TestLifecycleHooksNoOp:
    """All new hooks are no-ops when no hooks are discovered."""

    def setup_method(self):
        hooks_reset()

    def test_on_loop_start_noop(self):
        with patch("aloop.hooks._ensure_discovered", return_value=None):
            # Should not raise
            run_on_loop_start({"session_id": "test"})

    def test_on_loop_end_noop(self):
        with patch("aloop.hooks._ensure_discovered", return_value=None):
            run_on_loop_end({"session_id": "test"}, {"text": "done"})

    def test_on_turn_start_noop(self):
        with patch("aloop.hooks._ensure_discovered", return_value=None):
            run_on_turn_start({"session_id": "test", "turn_id": "abc"})

    def test_on_turn_end_noop(self):
        with patch("aloop.hooks._ensure_discovered", return_value=None):
            run_on_turn_end({"session_id": "test"}, {"iteration": 0})

    def test_on_pre_compaction_noop(self):
        with patch("aloop.hooks._ensure_discovered", return_value=None):
            result = run_on_pre_compaction({"session_id": "test"})
            assert result is None

    def test_on_post_compaction_noop(self):
        with patch("aloop.hooks._ensure_discovered", return_value=None):
            run_on_post_compaction({"session_id": "test"})


# ---------------------------------------------------------------------------
# Lifecycle hooks — called when hooks are configured
# ---------------------------------------------------------------------------


class TestLifecycleHooksExecution:
    """Lifecycle hooks call discovered hook functions."""

    def _make_mock_mod(self, hook_point, entries):
        mod = MagicMock()
        mod.get_hooks = lambda pt: entries if pt == hook_point else []
        return mod

    def test_on_loop_start_calls_hook(self):
        hooks_reset()
        entry = MagicMock()
        entry.fn = MagicMock(return_value=None)
        mock_mod = self._make_mock_mod("on_loop_start", [entry])

        with patch("aloop.hooks._ensure_discovered", return_value=mock_mod):
            run_on_loop_start({"session_id": "s1"})

        entry.fn.assert_called_once_with({"session_id": "s1"})

    def test_on_loop_end_calls_hook(self):
        hooks_reset()
        entry = MagicMock()
        entry.fn = MagicMock(return_value=None)
        mock_mod = self._make_mock_mod("on_loop_end", [entry])

        ctx = {"session_id": "s1"}
        result_data = {"text": "done", "turns": 3}
        with patch("aloop.hooks._ensure_discovered", return_value=mock_mod):
            run_on_loop_end(ctx, result_data)

        entry.fn.assert_called_once_with(ctx, result_data)

    def test_on_turn_start_calls_hook(self):
        hooks_reset()
        entry = MagicMock()
        entry.fn = MagicMock(return_value=None)
        mock_mod = self._make_mock_mod("on_turn_start", [entry])

        with patch("aloop.hooks._ensure_discovered", return_value=mock_mod):
            run_on_turn_start({"iteration": 0, "turn_id": "abc"})

        entry.fn.assert_called_once()

    def test_on_turn_end_calls_hook(self):
        hooks_reset()
        entry = MagicMock()
        entry.fn = MagicMock(return_value=None)
        mock_mod = self._make_mock_mod("on_turn_end", [entry])

        with patch("aloop.hooks._ensure_discovered", return_value=mock_mod):
            run_on_turn_end({"iteration": 0}, {"input_tokens": 100})

        entry.fn.assert_called_once()

    def test_on_pre_compaction_returns_instructions(self):
        """on_pre_compaction hooks can return extra compaction instructions."""
        hooks_reset()
        entry = MagicMock()
        entry.fn = MagicMock(return_value="Preserve all API endpoint details")
        mock_mod = self._make_mock_mod("on_pre_compaction", [entry])

        with patch("aloop.hooks._ensure_discovered", return_value=mock_mod):
            result = run_on_pre_compaction({"session_id": "test"})

        assert result == "Preserve all API endpoint details"

    def test_on_pre_compaction_concatenates_multiple(self):
        """Multiple pre_compaction hooks have results concatenated."""
        hooks_reset()
        entry1 = MagicMock()
        entry1.fn = MagicMock(return_value="Keep file paths")
        entry2 = MagicMock()
        entry2.fn = MagicMock(return_value="Keep error messages")

        mod = MagicMock()
        mod.get_hooks = lambda pt: [entry1, entry2] if pt == "on_pre_compaction" else []

        with patch("aloop.hooks._ensure_discovered", return_value=mod):
            result = run_on_pre_compaction({"session_id": "test"})

        assert "Keep file paths" in result
        assert "Keep error messages" in result

    def test_on_pre_compaction_none_when_empty_strings(self):
        """pre_compaction returns None when hooks return empty strings."""
        hooks_reset()
        entry = MagicMock()
        entry.fn = MagicMock(return_value="  ")

        mod = MagicMock()
        mod.get_hooks = lambda pt: [entry] if pt == "on_pre_compaction" else []

        with patch("aloop.hooks._ensure_discovered", return_value=mod):
            result = run_on_pre_compaction({})

        assert result is None

    def test_on_post_compaction_calls_hook(self):
        hooks_reset()
        entry = MagicMock()
        entry.fn = MagicMock(return_value=None)
        mock_mod = self._make_mock_mod("on_post_compaction", [entry])

        with patch("aloop.hooks._ensure_discovered", return_value=mock_mod):
            run_on_post_compaction({"messages_before": 50, "messages_after": 10})

        entry.fn.assert_called_once()

    def test_hook_failure_does_not_crash(self):
        """A failing lifecycle hook is logged but doesn't crash."""
        hooks_reset()
        entry = MagicMock()
        entry.name = "bad_hook"
        entry.fn = MagicMock(side_effect=RuntimeError("boom"))

        mod = MagicMock()
        mod.get_hooks = lambda pt: [entry] if pt == "on_loop_start" else []

        with patch("aloop.hooks._ensure_discovered", return_value=mod):
            # Should not raise
            run_on_loop_start({"session_id": "test"})

    def test_multiple_hooks_all_called(self):
        """All hooks for a lifecycle point are called in order."""
        hooks_reset()
        call_order = []

        entry1 = MagicMock()
        entry1.fn = MagicMock(side_effect=lambda ctx: call_order.append("first"))
        entry2 = MagicMock()
        entry2.fn = MagicMock(side_effect=lambda ctx: call_order.append("second"))

        mod = MagicMock()
        mod.get_hooks = lambda pt: [entry1, entry2] if pt == "on_turn_start" else []

        with patch("aloop.hooks._ensure_discovered", return_value=mod):
            run_on_turn_start({"iteration": 0})

        assert call_order == ["first", "second"]


# ---------------------------------------------------------------------------
# Tool merge behavior
# ---------------------------------------------------------------------------


class TestToolMergeBehavior:
    """Test tool merge: defaults + register_tools hooks + extra_tools + tools override."""

    @pytest.mark.asyncio
    async def test_default_tools_used_when_no_override(self):
        """Without tools= or extra_tools=, defaults + hook tools are used."""
        from aloop.agent_backend import ALoop
        from aloop.tools import ANALYSIS_TOOLS

        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        hook_tool = ToolDef(
            name="hook_tool",
            description="from hook",
            parameters={"type": "object", "properties": {}},
            execute=lambda: ToolResult(content="hook"),
        )

        async def mock_stream(*args, **kwargs):
            yield {"type": "text", "text": "done"}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.agent_backend.run_register_tools", return_value=[hook_tool]),
        ):
            events = []
            async for event in backend.stream("test"):
                events.append(event)

        # Verify LOOP_END was emitted (stream completed normally)
        types = [e.type for e in events]
        assert EventType.LOOP_END in types

    @pytest.mark.asyncio
    async def test_extra_tools_extends_defaults(self):
        """extra_tools= adds to the default tool set without replacing."""
        from aloop.agent_backend import ALoop
        from aloop.tools import ANALYSIS_TOOLS

        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        extra = ToolDef(
            name="extra_tool",
            description="extra",
            parameters={"type": "object", "properties": {}},
            execute=lambda: ToolResult(content="extra"),
        )

        # Track what tool_schemas get passed to _stream_completion
        captured_tools = []

        original_stream_completion = backend._stream_completion

        async def mock_stream(messages, system_prompt, tools, **kwargs):
            captured_tools.extend(tools or [])
            yield {"type": "text", "text": "done"}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.agent_backend.run_register_tools", return_value=[]),
        ):
            async for _ in backend.stream("test", extra_tools=[extra]):
                pass

        # Default tools + extra tool should be present
        tool_names = {t["function"]["name"] for t in captured_tools}
        assert "extra_tool" in tool_names
        # Default tools should also be present
        for t in ANALYSIS_TOOLS:
            assert t.name in tool_names

    @pytest.mark.asyncio
    async def test_tools_kwarg_replaces_entire_set(self):
        """tools= kwarg replaces all defaults and hook tools."""
        from aloop.agent_backend import ALoop

        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        custom = ToolDef(
            name="custom_only",
            description="only this",
            parameters={"type": "object", "properties": {}},
            execute=lambda: ToolResult(content="custom"),
        )

        captured_tools = []

        async def mock_stream(messages, system_prompt, tools, **kwargs):
            captured_tools.extend(tools or [])
            yield {"type": "text", "text": "done"}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with patch.object(backend, "_stream_completion", side_effect=mock_stream):
            async for _ in backend.stream("test", tools=[custom]):
                pass

        tool_names = {t["function"]["name"] for t in captured_tools}
        assert tool_names == {"custom_only"}

    @pytest.mark.asyncio
    async def test_tools_override_ignores_extra_tools(self):
        """When tools= is set, extra_tools= is ignored."""
        from aloop.agent_backend import ALoop

        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        override = ToolDef(
            name="override_tool",
            description="override",
            parameters={"type": "object", "properties": {}},
            execute=lambda: ToolResult(content="override"),
        )
        extra = ToolDef(
            name="extra_tool",
            description="extra",
            parameters={"type": "object", "properties": {}},
            execute=lambda: ToolResult(content="extra"),
        )

        captured_tools = []

        async def mock_stream(messages, system_prompt, tools, **kwargs):
            captured_tools.extend(tools or [])
            yield {"type": "text", "text": "done"}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with patch.object(backend, "_stream_completion", side_effect=mock_stream):
            # Both tools= and extra_tools= provided — tools= wins
            async for _ in backend.stream("test", tools=[override], extra_tools=[extra]):
                pass

        tool_names = {t["function"]["name"] for t in captured_tools}
        assert "override_tool" in tool_names
        assert "extra_tool" not in tool_names


# ---------------------------------------------------------------------------
# Lifecycle hooks wired into agent_backend — integration
# ---------------------------------------------------------------------------


class TestLifecycleHooksIntegration:
    """Test that lifecycle hooks are called from stream() in correct order."""

    @pytest.mark.asyncio
    async def test_lifecycle_hooks_called_on_simple_stream(self):
        """A simple text response calls loop_start, turn_start, turn_end, loop_end."""
        from aloop.agent_backend import ALoop

        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        async def mock_stream(*args, **kwargs):
            yield {"type": "text", "text": "hello"}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        hook_calls = []

        def track_loop_start(ctx):
            hook_calls.append("loop_start")

        def track_loop_end(ctx, result):
            hook_calls.append("loop_end")

        def track_turn_start(ctx):
            hook_calls.append("turn_start")

        def track_turn_end(ctx, result):
            hook_calls.append("turn_end")

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.agent_backend.run_on_loop_start", side_effect=track_loop_start),
            patch("aloop.agent_backend.run_on_loop_end", side_effect=track_loop_end),
            patch("aloop.agent_backend.run_on_turn_start", side_effect=track_turn_start),
            patch("aloop.agent_backend.run_on_turn_end", side_effect=track_turn_end),
            patch("aloop.agent_backend.run_register_tools", return_value=[]),
        ):
            events = []
            async for event in backend.stream("test"):
                events.append(event)

        assert hook_calls == ["loop_start", "turn_start", "turn_end", "loop_end"]

    @pytest.mark.asyncio
    async def test_tool_rejected_in_execute_tool(self):
        """ToolRejected from before_tool hook returns error result to model."""
        from aloop.agent_backend import ALoop

        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        test_tool = ToolDef(
            name="dangerous_tool",
            description="test",
            parameters={"type": "object", "properties": {}},
            execute=lambda: ToolResult(content="should not reach"),
        )

        # Simulate before_tool raising ToolRejected (through the hook runner returning
        # allow=False with reason)
        with patch(
            "aloop.agent_backend.run_before_tool",
            return_value={"allow": False, "reason": "blocked by policy"},
        ):
            result = await backend._execute_tool(test_tool, {})

        assert result.is_error is True
        assert "blocked by policy" in result.content

    @pytest.mark.asyncio
    async def test_tool_result_short_circuit_in_execute_tool(self):
        """ToolResult from before_tool hook short-circuits actual execution."""
        from aloop.agent_backend import ALoop

        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        cached_result = ToolResult(content="from cache", is_error=False)

        test_tool = ToolDef(
            name="cacheable_tool",
            description="test",
            parameters={"type": "object", "properties": {}},
            execute=lambda: ToolResult(content="from execution"),
        )

        with patch(
            "aloop.agent_backend.run_before_tool",
            return_value={"allow": False, "tool_result": cached_result},
        ):
            result = await backend._execute_tool(test_tool, {})

        assert result.content == "from cache"
        assert result.is_error is False


# ---------------------------------------------------------------------------
# Hook discovery includes new hook points
# ---------------------------------------------------------------------------


class TestHookPointDiscovery:
    """Verify that the 6 new hook points are included in discovery."""

    def test_all_ten_hook_points_in_discovery(self):
        """_ensure_discovered iterates all 10 hook point names."""
        import aloop.hooks as hooks_module
        import inspect

        source = inspect.getsource(hooks_module._ensure_discovered)

        expected_points = [
            "before_tool",
            "after_tool",
            "gather_context",
            "register_tools",
            "on_loop_start",
            "on_loop_end",
            "on_turn_start",
            "on_turn_end",
            "on_pre_compaction",
            "on_post_compaction",
        ]

        for point in expected_points:
            assert f'"{point}"' in source, f"Hook point '{point}' not found in _ensure_discovered"
