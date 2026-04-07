"""Tests for build_agent_tool() — the agent tool factory."""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock

import pytest

from aloop.agent_result import AgentResult, FORK_BOILERPLATE
from aloop.executor import AgentExecutionHandle
from aloop.tools.agent import build_agent_tool, _format_mode_listing
from aloop.tools_base import ToolDef, ToolResult


# ---------------------------------------------------------------------------
# Fake executor for tool tests
# ---------------------------------------------------------------------------


class FakeExecutor:
    def __init__(self, *, next_result: AgentResult | None = None, raise_exc: Exception | None = None):
        self.spawn_calls: list[dict] = []
        self.next_result = next_result
        self.raise_exc = raise_exc

    async def spawn(self, **kwargs) -> AgentExecutionHandle:
        self.spawn_calls.append(kwargs)
        result = self.next_result or AgentResult(
            text="default child output",
            session_id="child_default",
            spawn_kind="fork" if kwargs.get("fork") else "fresh",
            mode=kwargs.get("mode"),
            parent_session_id=kwargs.get("parent_session_id"),
            parent_turn_id=kwargs.get("parent_turn_id"),
            usage={"input_tokens": 1, "output_tokens": 2, "turns": 1},
        )
        exc = self.raise_exc

        async def _ret():
            if exc is not None:
                raise exc
            return result

        return AgentExecutionHandle(
            session_id=result.session_id,
            spawn_kind=result.spawn_kind,
            mode=result.mode,
            parent_session_id=kwargs.get("parent_session_id"),
            parent_turn_id=kwargs.get("parent_turn_id"),
            _task=asyncio.create_task(_ret()),
        )


def _basic_modes() -> dict:
    return {
        "orchestrator": {"can_fork": True, "spawnable_modes": ["explore", "reviewer"]},
        "explore": {
            "subagent_eligible": True,
            "tools": ["read_file", "grep"],
            "system_prompt": "Read-only exploration mode.",
        },
        "reviewer": {
            "subagent_eligible": True,
            "tools": ["*"],
            "system_prompt": "Code reviewer.",
        },
        "ineligible_mode": {
            "tools": ["bash"],
        },
    }


# ---------------------------------------------------------------------------
# Tool construction
# ---------------------------------------------------------------------------


class TestBuildAgentTool:
    def test_returns_tooldef_with_name_agent(self):
        tool = build_agent_tool(
            spawnable_modes=["explore"],
            can_fork=True,
            all_modes=_basic_modes(),
            parent_loop=MagicMock(),
            executor=FakeExecutor(),
        )
        assert isinstance(tool, ToolDef)
        assert tool.name == "agent"

    def test_description_lists_spawnable_modes(self):
        tool = build_agent_tool(
            spawnable_modes=["explore", "reviewer"],
            can_fork=True,
            all_modes=_basic_modes(),
            parent_loop=MagicMock(),
            executor=FakeExecutor(),
        )
        assert "explore" in tool.description
        assert "reviewer" in tool.description

    def test_description_says_none_when_empty_spawnable(self):
        tool = build_agent_tool(
            spawnable_modes=[],
            can_fork=True,
            all_modes={},
            parent_loop=MagicMock(),
            executor=FakeExecutor(),
        )
        assert "none" in tool.description.lower()

    def test_description_warns_when_can_fork_false(self):
        tool = build_agent_tool(
            spawnable_modes=["explore"],
            can_fork=False,
            all_modes=_basic_modes(),
            parent_loop=MagicMock(),
            executor=FakeExecutor(),
        )
        assert "fork" in tool.description.lower()
        assert "disabled" in tool.description.lower() or "must specify" in tool.description.lower()

    def test_schema_has_required_prompt_and_description(self):
        tool = build_agent_tool(
            spawnable_modes=["explore"], can_fork=True,
            all_modes=_basic_modes(), parent_loop=MagicMock(), executor=FakeExecutor(),
        )
        params = tool.parameters
        assert "prompt" in params["properties"]
        assert "description" in params["properties"]
        assert "prompt" in params["required"]
        assert "description" in params["required"]

    def test_schema_mode_and_model_optional(self):
        tool = build_agent_tool(
            spawnable_modes=["explore"], can_fork=True,
            all_modes=_basic_modes(), parent_loop=MagicMock(), executor=FakeExecutor(),
        )
        params = tool.parameters
        assert "mode" in params["properties"]
        assert "model" in params["properties"]
        assert "mode" not in params["required"]
        assert "model" not in params["required"]

    def test_execute_signature_has_underscore_context(self):
        # The agent tool relies on _context injection by _execute_tool.
        # Verify that inspect.signature picks up _context.
        tool = build_agent_tool(
            spawnable_modes=["explore"], can_fork=True,
            all_modes=_basic_modes(), parent_loop=MagicMock(), executor=FakeExecutor(),
        )
        sig = inspect.signature(tool.execute)
        assert "_context" in sig.parameters

    def test_description_includes_current_mode_name(self):
        tool = build_agent_tool(
            spawnable_modes=["explore"], can_fork=True,
            all_modes=_basic_modes(), parent_loop=MagicMock(), executor=FakeExecutor(),
            current_mode_name="orchestrator",
        )
        assert "orchestrator" in tool.description


# ---------------------------------------------------------------------------
# Format mode listing helper
# ---------------------------------------------------------------------------


class TestFormatModeListing:
    def test_empty_list(self):
        out = _format_mode_listing([], {})
        assert "none" in out.lower()

    def test_lists_modes_with_tools_and_first_line(self):
        modes = {
            "explore": {
                "tools": ["read_file"],
                "system_prompt": "First line desc.\nSecond line",
            }
        }
        out = _format_mode_listing(["explore"], modes)
        assert "explore" in out
        assert "First line desc" in out
        assert "read_file" in out

    def test_handles_star_tools(self):
        modes = {"all": {"tools": ["*"]}}
        out = _format_mode_listing(["all"], modes)
        assert "all tools" in out

    def test_no_system_prompt_uses_placeholder(self):
        modes = {"x": {}}
        out = _format_mode_listing(["x"], modes)
        assert "no description" in out


# ---------------------------------------------------------------------------
# Fork-path validation
# ---------------------------------------------------------------------------


class TestForkPathValidation:
    @pytest.mark.asyncio
    async def test_fork_succeeds_with_persistent_parent(self):
        executor = FakeExecutor()
        tool = build_agent_tool(
            spawnable_modes=["explore"], can_fork=True,
            all_modes=_basic_modes(), parent_loop=MagicMock(), executor=executor,
        )
        result = await tool.execute(
            prompt="explore the auth module",
            description="explore auth",
            _context={"session_id": "p1", "turn_id": "t1"},
        )
        assert isinstance(result, ToolResult)
        assert not result.is_error
        assert len(executor.spawn_calls) == 1
        call = executor.spawn_calls[0]
        assert call["fork"] is True
        assert call["mode"] is None
        assert call["parent_session_id"] == "p1"
        assert call["parent_turn_id"] == "t1"

    @pytest.mark.asyncio
    async def test_fork_blocked_when_can_fork_false(self):
        executor = FakeExecutor()
        tool = build_agent_tool(
            spawnable_modes=["explore"], can_fork=False,
            all_modes=_basic_modes(), parent_loop=MagicMock(), executor=executor,
        )
        result = await tool.execute(
            prompt="x",
            description="d",
            _context={"session_id": "p1", "turn_id": "t1"},
        )
        assert result.is_error
        assert "fork" in result.content.lower()
        assert len(executor.spawn_calls) == 0

    @pytest.mark.asyncio
    async def test_fork_blocked_when_no_parent_session(self):
        executor = FakeExecutor()
        tool = build_agent_tool(
            spawnable_modes=["explore"], can_fork=True,
            all_modes=_basic_modes(), parent_loop=MagicMock(), executor=executor,
        )
        result = await tool.execute(
            prompt="x", description="d",
            _context={"turn_id": "t1"},  # missing session_id
        )
        assert result.is_error
        assert "persistent parent" in result.content.lower()

    @pytest.mark.asyncio
    async def test_fork_blocked_when_no_parent_turn_id(self):
        executor = FakeExecutor()
        tool = build_agent_tool(
            spawnable_modes=["explore"], can_fork=True,
            all_modes=_basic_modes(), parent_loop=MagicMock(), executor=executor,
        )
        result = await tool.execute(
            prompt="x", description="d",
            _context={"session_id": "p1"},  # missing turn_id
        )
        assert result.is_error

    @pytest.mark.asyncio
    async def test_fork_with_no_context_returns_error(self):
        executor = FakeExecutor()
        tool = build_agent_tool(
            spawnable_modes=["explore"], can_fork=True,
            all_modes=_basic_modes(), parent_loop=MagicMock(), executor=executor,
        )
        result = await tool.execute(prompt="x", description="d", _context=None)
        assert result.is_error


# ---------------------------------------------------------------------------
# Fresh-path validation
# ---------------------------------------------------------------------------


class TestFreshPathValidation:
    @pytest.mark.asyncio
    async def test_fresh_succeeds_with_eligible_mode_in_spawnable(self):
        executor = FakeExecutor()
        tool = build_agent_tool(
            spawnable_modes=["explore"], can_fork=True,
            all_modes=_basic_modes(), parent_loop=MagicMock(), executor=executor,
        )
        result = await tool.execute(
            prompt="explore auth.py",
            description="explore",
            mode="explore",
            _context={"session_id": "p1", "turn_id": "t1"},
        )
        assert not result.is_error
        call = executor.spawn_calls[0]
        assert call["fork"] is False
        assert call["mode"] == "explore"

    @pytest.mark.asyncio
    async def test_fresh_blocked_when_mode_not_in_spawnable(self):
        executor = FakeExecutor()
        tool = build_agent_tool(
            spawnable_modes=["explore"], can_fork=True,
            all_modes=_basic_modes(), parent_loop=MagicMock(), executor=executor,
        )
        result = await tool.execute(
            prompt="x", description="d",
            mode="reviewer",  # not in spawnable
            _context={"session_id": "p1", "turn_id": "t1"},
        )
        assert result.is_error
        assert "spawnable_modes" in result.content

    @pytest.mark.asyncio
    async def test_fresh_blocked_when_target_mode_not_eligible(self):
        executor = FakeExecutor()
        # spawnable_modes lists ineligible_mode but it's not subagent_eligible
        tool = build_agent_tool(
            spawnable_modes=["ineligible_mode"], can_fork=True,
            all_modes=_basic_modes(), parent_loop=MagicMock(), executor=executor,
        )
        result = await tool.execute(
            prompt="x", description="d",
            mode="ineligible_mode",
            _context={"session_id": "p1", "turn_id": "t1"},
        )
        assert result.is_error
        assert "subagent_eligible" in result.content

    @pytest.mark.asyncio
    async def test_fresh_blocked_when_target_mode_unknown(self):
        executor = FakeExecutor()
        tool = build_agent_tool(
            spawnable_modes=["nonexistent"], can_fork=True,
            all_modes=_basic_modes(), parent_loop=MagicMock(), executor=executor,
        )
        result = await tool.execute(
            prompt="x", description="d",
            mode="nonexistent",
            _context={"session_id": "p1", "turn_id": "t1"},
        )
        assert result.is_error
        assert "not defined" in result.content or "spawnable_modes" in result.content

    @pytest.mark.asyncio
    async def test_fresh_propagates_model_override(self):
        executor = FakeExecutor()
        tool = build_agent_tool(
            spawnable_modes=["explore"], can_fork=True,
            all_modes=_basic_modes(), parent_loop=MagicMock(), executor=executor,
        )
        result = await tool.execute(
            prompt="x", description="d",
            mode="explore", model="claude-opus-4",
            _context={"session_id": "p1", "turn_id": "t1"},
        )
        assert not result.is_error
        call = executor.spawn_calls[0]
        assert call["model"] == "claude-opus-4"


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------


class TestResultFormatting:
    @pytest.mark.asyncio
    async def test_result_includes_child_text(self):
        executor = FakeExecutor(next_result=AgentResult(
            text="my child output",
            session_id="child42",
            spawn_kind="fork",
            usage={"input_tokens": 100, "output_tokens": 50, "turns": 2},
        ))
        tool = build_agent_tool(
            spawnable_modes=[], can_fork=True,
            all_modes={}, parent_loop=MagicMock(), executor=executor,
        )
        result = await tool.execute(
            prompt="x", description="d",
            _context={"session_id": "p", "turn_id": "t"},
        )
        assert "my child output" in result.content

    @pytest.mark.asyncio
    async def test_result_includes_lineage_footer(self):
        executor = FakeExecutor(next_result=AgentResult(
            text="hi",
            session_id="child99",
            spawn_kind="fork",
            usage={"input_tokens": 7, "output_tokens": 3, "turns": 1},
        ))
        tool = build_agent_tool(
            spawnable_modes=[], can_fork=True,
            all_modes={}, parent_loop=MagicMock(), executor=executor,
        )
        result = await tool.execute(
            prompt="x", description="d",
            _context={"session_id": "p", "turn_id": "t"},
        )
        assert "child99" in result.content
        assert "fork" in result.content
        assert "in: 7" in result.content
        assert "out: 3" in result.content

    @pytest.mark.asyncio
    async def test_result_details_carries_structured_data(self):
        executor = FakeExecutor(next_result=AgentResult(
            text="hi",
            session_id="child_xyz",
            spawn_kind="fresh",
            mode="explore",
            usage={"input_tokens": 1, "output_tokens": 1, "turns": 1},
        ))
        tool = build_agent_tool(
            spawnable_modes=["explore"], can_fork=True,
            all_modes=_basic_modes(), parent_loop=MagicMock(), executor=executor,
        )
        result = await tool.execute(
            prompt="x", description="d",
            mode="explore",
            _context={"session_id": "p", "turn_id": "t"},
        )
        assert result.details is not None
        assert result.details["session_id"] == "child_xyz"
        assert result.details["spawn_kind"] == "fresh"
        assert result.details["mode"] == "explore"
        assert "usage" in result.details

    @pytest.mark.asyncio
    async def test_result_handles_empty_child_text(self):
        executor = FakeExecutor(next_result=AgentResult(
            text="",
            session_id="empty_child",
            spawn_kind="fork",
            usage={},
        ))
        tool = build_agent_tool(
            spawnable_modes=[], can_fork=True,
            all_modes={}, parent_loop=MagicMock(), executor=executor,
        )
        result = await tool.execute(
            prompt="x", description="d",
            _context={"session_id": "p", "turn_id": "t"},
        )
        assert "no text" in result.content.lower()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_handles_executor_exception(self):
        executor = FakeExecutor(raise_exc=RuntimeError("boom"))
        tool = build_agent_tool(
            spawnable_modes=[], can_fork=True,
            all_modes={}, parent_loop=MagicMock(), executor=executor,
        )
        result = await tool.execute(
            prompt="x", description="d",
            _context={"session_id": "p", "turn_id": "t"},
        )
        assert result.is_error
        assert "boom" in result.content
