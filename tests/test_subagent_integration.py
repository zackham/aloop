"""End-to-end integration tests for subagents — agent tool injection,
fork-path round trips, fresh-path round trips, and recursive spawning.

These tests use a real ALoop instance with `_stream_completion` patched
to feed scripted parent + child responses, plus `_load_aloop_config`
patched to return a known modes config. They verify the cross-cutting
plumbing in agent_backend.py.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from aloop.agent_backend import ALoop
from aloop.session import AgentSession
from aloop.types import EventType


# ---------------------------------------------------------------------------
# Helpers — scripted stream responses
# ---------------------------------------------------------------------------


_TEXT_CHUNK_USAGE = {"prompt_tokens": 5, "completion_tokens": 5}


async def _emit_text_completion(text: str):
    """Emit a simple completion sequence (text + usage) inside an async gen."""
    yield {"type": "text", "text": text}
    yield {"type": "usage", "usage": dict(_TEXT_CHUNK_USAGE)}


def _tool_call_then_text(tool_name: str, args: dict, follow_up_text: str):
    """Build a callable that returns a sequence of streams.

    First call: assistant calls tool_name with args.
    Second call: assistant emits follow_up_text.
    """
    calls = {"n": 0}

    async def mock_stream(messages, system_prompt, tools, **kw):
        n = calls["n"]
        calls["n"] += 1
        if n == 0:
            yield {
                "type": "tool_call_delta",
                "index": 0,
                "id": "tc_1",
                "function": {"name": tool_name, "arguments": json.dumps(args)},
            }
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        else:
            yield {"type": "text", "text": follow_up_text}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    return mock_stream, calls


# ---------------------------------------------------------------------------
# Tool injection
# ---------------------------------------------------------------------------


class TestAgentToolInjection:
    @pytest.mark.asyncio
    async def test_injected_when_mode_has_spawnable_modes(self, tmp_path):
        config = {
            "modes": {
                "orchestrator": {
                    "spawnable_modes": ["explore"],
                    "tools": ["read_file"],
                },
                "explore": {"subagent_eligible": True, "tools": ["read_file"]},
            }
        }
        backend = ALoop(model="minimax-m2.5", api_key="test-key")
        captured_tools = []

        async def mock_stream(messages, system_prompt, tools, **kw):
            captured_tools.append(tools)
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": dict(_TEXT_CHUNK_USAGE)}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.session._sessions_dir", return_value=tmp_path / "sessions"),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            async for _ in backend.stream("hi", mode="orchestrator", persist_session=False):
                pass

        names = [t["function"]["name"] for t in captured_tools[0]]
        assert "agent" in names

    @pytest.mark.asyncio
    async def test_injected_when_mode_has_can_fork_true(self, tmp_path):
        config = {
            "modes": {
                "fork_only": {"can_fork": True, "tools": ["read_file"]},
            }
        }
        backend = ALoop(model="minimax-m2.5", api_key="test-key")
        captured_tools = []

        async def mock_stream(messages, system_prompt, tools, **kw):
            captured_tools.append(tools)
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": dict(_TEXT_CHUNK_USAGE)}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.session._sessions_dir", return_value=tmp_path / "sessions"),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            async for _ in backend.stream("hi", mode="fork_only", persist_session=False):
                pass

        names = [t["function"]["name"] for t in captured_tools[0]]
        assert "agent" in names

    @pytest.mark.asyncio
    async def test_NOT_injected_when_neither_spawnable_nor_can_fork(self, tmp_path):
        config = {
            "modes": {
                "plain": {"tools": ["read_file"]},
            }
        }
        backend = ALoop(model="minimax-m2.5", api_key="test-key")
        captured_tools = []

        async def mock_stream(messages, system_prompt, tools, **kw):
            captured_tools.append(tools)
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": dict(_TEXT_CHUNK_USAGE)}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.session._sessions_dir", return_value=tmp_path / "sessions"),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            async for _ in backend.stream("hi", mode="plain", persist_session=False):
                pass

        names = [t["function"]["name"] for t in captured_tools[0]]
        assert "agent" not in names

    @pytest.mark.asyncio
    async def test_injected_when_mode_has_tools_star(self, tmp_path):
        config = {
            "modes": {
                "all_tools": {
                    "tools": ["*"],
                    "can_fork": True,
                },
            }
        }
        backend = ALoop(model="minimax-m2.5", api_key="test-key")
        captured_tools = []

        async def mock_stream(messages, system_prompt, tools, **kw):
            captured_tools.append(tools)
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": dict(_TEXT_CHUNK_USAGE)}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.session._sessions_dir", return_value=tmp_path / "sessions"),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            async for _ in backend.stream("hi", mode="all_tools", persist_session=False):
                pass

        names = [t["function"]["name"] for t in captured_tools[0]]
        assert "agent" in names

    @pytest.mark.asyncio
    async def test_NOT_double_injected_when_already_present(self, tmp_path):
        from aloop.tools_base import ToolDef, ToolResult

        async def _noop(**kw):
            return ToolResult(content="ok")

        custom_agent_tool = ToolDef(
            name="agent",
            description="user-supplied agent tool",
            parameters={"type": "object"},
            execute=_noop,
        )

        config = {
            "modes": {
                "orch": {
                    "spawnable_modes": ["explore"],
                    "tools": ["read_file"],
                },
                "explore": {"subagent_eligible": True},
            }
        }
        backend = ALoop(model="minimax-m2.5", api_key="test-key")
        captured_tools = []

        async def mock_stream(messages, system_prompt, tools, **kw):
            captured_tools.append(tools)
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": dict(_TEXT_CHUNK_USAGE)}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.session._sessions_dir", return_value=tmp_path / "sessions"),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            async for _ in backend.stream(
                "hi",
                mode="orch",
                extra_tools=[custom_agent_tool],
                persist_session=False,
            ):
                pass

        names = [t["function"]["name"] for t in captured_tools[0]]
        assert names.count("agent") == 1


# ---------------------------------------------------------------------------
# Fork path end-to-end
# ---------------------------------------------------------------------------


class TestForkPathEndToEnd:
    @pytest.mark.asyncio
    async def test_fork_spawn_creates_child_session_with_parent_pointer(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        config = {
            "modes": {
                "orch": {
                    "can_fork": True,
                    "tools": ["read_file"],
                },
            }
        }

        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        # Track parent's stream call count so first call returns tool_call,
        # second returns text. The child runs inside the agent tool which
        # invokes parent_loop.stream(fork_from=...) — that recursive call
        # also goes through this same mock_stream. We need to differentiate
        # based on whether messages include the fork boilerplate.
        call_count = {"n": 0}

        async def mock_stream(messages, system_prompt, tools, **kw):
            n = call_count["n"]
            call_count["n"] += 1
            # Check if this is the child run (last user message starts with
            # the FORK_BOILERPLATE)
            from aloop.agent_result import FORK_BOILERPLATE
            last_user = next(
                (m for m in reversed(messages) if m.get("role") == "user"), None
            )
            is_child = last_user and isinstance(last_user.get("content"), str) and last_user["content"].startswith(FORK_BOILERPLATE)

            if is_child:
                # Child returns text immediately
                yield {"type": "text", "text": "child explored auth.py"}
                yield {"type": "usage", "usage": {"prompt_tokens": 8, "completion_tokens": 4}}
                return

            # Parent: first call emits tool call, second emits final text
            if n == 0:
                yield {
                    "type": "tool_call_delta",
                    "index": 0,
                    "id": "tc_1",
                    "function": {
                        "name": "agent",
                        "arguments": json.dumps({
                            "prompt": "explore the auth module",
                            "description": "explore auth",
                        }),
                    },
                }
                yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
            else:
                yield {"type": "text", "text": "summary based on child"}
                yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.session._sessions_dir", return_value=sessions_dir),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            events = []
            async for event in backend.stream(
                "explore for me",
                mode="orch",
                session_id="parent_session_1",
            ):
                events.append(event)

        # Assert a child session was created with fork_from pointing to parent
        ctx_files = list(sessions_dir.glob("*.context.json"))
        # Should have at least 2 context files (parent + child)
        assert len(ctx_files) >= 2
        child_sessions = []
        for f in ctx_files:
            data = json.loads(f.read_text())
            if data.get("fork_from") == "parent_session_1":
                child_sessions.append(data)
        assert len(child_sessions) == 1
        child = child_sessions[0]
        assert child["fork_from"] == "parent_session_1"
        # Spawn metadata should be persisted
        assert child.get("spawn_metadata") is not None
        assert child["spawn_metadata"]["kind"] == "fork"
        assert child["spawn_metadata"]["parent_session_id"] == "parent_session_1"
        assert child["spawn_metadata"]["spawning_mode"] == "orch"

    @pytest.mark.asyncio
    async def test_fork_spawn_returns_child_text_to_parent(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        config = {"modes": {"orch": {"can_fork": True, "tools": ["read_file"]}}}
        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        call_count = {"n": 0}

        async def mock_stream(messages, system_prompt, tools, **kw):
            n = call_count["n"]
            call_count["n"] += 1
            from aloop.agent_result import FORK_BOILERPLATE
            last_user = next(
                (m for m in reversed(messages) if m.get("role") == "user"), None
            )
            is_child = last_user and last_user.get("content", "").startswith(FORK_BOILERPLATE)

            if is_child:
                yield {"type": "text", "text": "CHILD_OUTPUT_MARKER"}
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
                return

            if n == 0:
                yield {
                    "type": "tool_call_delta",
                    "index": 0,
                    "id": "tc_1",
                    "function": {
                        "name": "agent",
                        "arguments": json.dumps({"prompt": "explore", "description": "x"}),
                    },
                }
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
                return

            # Parent's second call should see the CHILD_OUTPUT_MARKER in
            # the tool result message.
            tool_msgs = [m for m in messages if m.get("role") == "tool"]
            assert any("CHILD_OUTPUT_MARKER" in (m.get("content") or "") for m in tool_msgs)
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.session._sessions_dir", return_value=sessions_dir),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            async for _ in backend.stream("go", mode="orch", session_id="parent_2"):
                pass


# ---------------------------------------------------------------------------
# Fresh path end-to-end
# ---------------------------------------------------------------------------


class TestFreshPathEndToEnd:
    @pytest.mark.asyncio
    async def test_fresh_spawn_creates_independent_child_session(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        config = {
            "modes": {
                "orch": {
                    "spawnable_modes": ["reviewer"],
                    "tools": ["read_file"],
                },
                "reviewer": {
                    "subagent_eligible": True,
                    "tools": ["read_file"],
                    "system_prompt": "You are a reviewer.",
                },
            }
        }

        backend = ALoop(model="minimax-m2.5", api_key="test-key")
        call_count = {"n": 0}

        # Patch at the class level so the fresh-path child ALoop also sees it.
        async def mock_stream(self, messages, system_prompt, tools, **kw):
            n = call_count["n"]
            call_count["n"] += 1
            # Reviewer mode → distinct system prompt
            if system_prompt == "You are a reviewer.":
                yield {"type": "text", "text": "review done"}
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
                return

            if n == 0:
                yield {
                    "type": "tool_call_delta",
                    "index": 0,
                    "id": "tc_1",
                    "function": {
                        "name": "agent",
                        "arguments": json.dumps({
                            "prompt": "review auth.py",
                            "description": "review",
                            "mode": "reviewer",
                        }),
                    },
                }
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
            else:
                yield {"type": "text", "text": "summary"}
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 5}}

        with (
            patch("aloop.agent_backend.ALoop._stream_completion", new=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.session._sessions_dir", return_value=sessions_dir),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            async for _ in backend.stream(
                "review the auth module",
                mode="orch",
                session_id="parent_fresh",
            ):
                pass

        # Find child sessions (with spawn_metadata.kind == "fresh")
        ctx_files = list(sessions_dir.glob("*.context.json"))
        fresh_children = []
        for f in ctx_files:
            data = json.loads(f.read_text())
            sm = data.get("spawn_metadata") or {}
            if sm.get("kind") == "fresh":
                fresh_children.append(data)
        assert len(fresh_children) == 1
        child = fresh_children[0]
        assert child["spawn_metadata"]["child_mode"] == "reviewer"
        assert child["spawn_metadata"]["spawning_mode"] == "orch"
        # Fresh children do NOT have fork_from set
        assert child.get("fork_from") is None


# ---------------------------------------------------------------------------
# Permission boundary
# ---------------------------------------------------------------------------


class TestSubagentPermissionBoundary:
    @pytest.mark.asyncio
    async def test_fresh_spawn_into_readonly_mode_only_sees_readonly_tools(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        config = {
            "modes": {
                "orch": {
                    "spawnable_modes": ["explore"],
                    "tools": ["read_file"],
                },
                "explore": {
                    "subagent_eligible": True,
                    "tools": ["read_file", "grep"],
                    "system_prompt": "Explore mode.",
                },
            }
        }

        backend = ALoop(model="minimax-m2.5", api_key="test-key")
        child_tools_seen: list[list[dict]] = []
        call_count = {"n": 0}

        async def mock_stream(self, messages, system_prompt, tools, **kw):
            n = call_count["n"]
            call_count["n"] += 1

            if system_prompt == "Explore mode.":
                child_tools_seen.append(tools)
                yield {"type": "text", "text": "explore done"}
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
                return

            if n == 0:
                yield {
                    "type": "tool_call_delta",
                    "index": 0,
                    "id": "tc_1",
                    "function": {
                        "name": "agent",
                        "arguments": json.dumps({
                            "prompt": "look at auth.py",
                            "description": "x",
                            "mode": "explore",
                        }),
                    },
                }
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
            else:
                yield {"type": "text", "text": "ok"}
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 5}}

        with (
            patch("aloop.agent_backend.ALoop._stream_completion", new=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.session._sessions_dir", return_value=sessions_dir),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            async for _ in backend.stream("explore", mode="orch", session_id="parent3"):
                pass

        assert len(child_tools_seen) == 1
        names = {t["function"]["name"] for t in child_tools_seen[0]}
        assert "read_file" in names
        assert "grep" in names
        assert "write_file" not in names
        assert "bash" not in names


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestSubagentEdgeCases:
    @pytest.mark.asyncio
    async def test_fork_when_persist_session_false_returns_error_to_model(self, tmp_path):
        config = {"modes": {"orch": {"can_fork": True, "tools": ["read_file"]}}}
        backend = ALoop(model="minimax-m2.5", api_key="test-key")
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        call_count = {"n": 0}
        captured_tool_results: list[str] = []

        async def mock_stream(messages, system_prompt, tools, **kw):
            n = call_count["n"]
            call_count["n"] += 1
            # Capture tool results from message history
            for m in messages:
                if m.get("role") == "tool":
                    captured_tool_results.append(m.get("content", ""))
            if n == 0:
                yield {
                    "type": "tool_call_delta",
                    "index": 0,
                    "id": "tc_1",
                    "function": {
                        "name": "agent",
                        "arguments": json.dumps({"prompt": "x", "description": "y"}),
                    },
                }
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
            else:
                yield {"type": "text", "text": "ok"}
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.session._sessions_dir", return_value=sessions_dir),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            async for _ in backend.stream(
                "go",
                mode="orch",
                persist_session=False,
            ):
                pass

        # Tool result should contain an error about persistent parent session
        assert any("persistent parent" in r for r in captured_tool_results)

    @pytest.mark.asyncio
    async def test_spawn_unknown_mode_returns_error_to_model(self, tmp_path):
        config = {
            "modes": {
                "orch": {
                    "spawnable_modes": ["valid"],
                    "tools": ["read_file"],
                },
                "valid": {"subagent_eligible": True},
            }
        }
        backend = ALoop(model="minimax-m2.5", api_key="test-key")
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        captured_tool_results: list[str] = []
        call_count = {"n": 0}

        async def mock_stream(messages, system_prompt, tools, **kw):
            n = call_count["n"]
            call_count["n"] += 1
            for m in messages:
                if m.get("role") == "tool":
                    captured_tool_results.append(m.get("content", ""))
            if n == 0:
                yield {
                    "type": "tool_call_delta",
                    "index": 0,
                    "id": "tc_1",
                    "function": {
                        "name": "agent",
                        "arguments": json.dumps({
                            "prompt": "x",
                            "description": "y",
                            "mode": "ghost",  # not in spawnable_modes
                        }),
                    },
                }
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
            else:
                yield {"type": "text", "text": "ok"}
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.session._sessions_dir", return_value=sessions_dir),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            async for _ in backend.stream("go", mode="orch", session_id="p1"):
                pass

        assert any("spawnable_modes" in r for r in captured_tool_results)

    @pytest.mark.asyncio
    async def test_spawn_non_eligible_mode_returns_error_to_model(self, tmp_path):
        config = {
            "modes": {
                "orch": {
                    "spawnable_modes": ["target"],
                    "tools": ["read_file"],
                },
                "target": {},  # missing subagent_eligible
            }
        }
        backend = ALoop(model="minimax-m2.5", api_key="test-key")
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        captured_tool_results: list[str] = []
        call_count = {"n": 0}

        async def mock_stream(messages, system_prompt, tools, **kw):
            n = call_count["n"]
            call_count["n"] += 1
            for m in messages:
                if m.get("role") == "tool":
                    captured_tool_results.append(m.get("content", ""))
            if n == 0:
                yield {
                    "type": "tool_call_delta",
                    "index": 0,
                    "id": "tc_1",
                    "function": {
                        "name": "agent",
                        "arguments": json.dumps({
                            "prompt": "x",
                            "description": "y",
                            "mode": "target",
                        }),
                    },
                }
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
            else:
                yield {"type": "text", "text": "ok"}
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.session._sessions_dir", return_value=sessions_dir),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            async for _ in backend.stream("go", mode="orch", session_id="p1"):
                pass

        assert any("subagent_eligible" in r for r in captured_tool_results)


# ---------------------------------------------------------------------------
# turn_id injection into _context
# ---------------------------------------------------------------------------


class TestTurnIdInjection:
    @pytest.mark.asyncio
    async def test_tool_receives_turn_id_in_context(self, tmp_path):
        from aloop.tools_base import ToolDef, ToolResult

        captured: dict = {}

        async def my_tool(prompt: str, _context: dict | None = None) -> ToolResult:
            captured["context"] = _context
            return ToolResult(content="done")

        td = ToolDef(
            name="capture",
            description="capture context",
            parameters={
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
                "required": ["prompt"],
            },
            execute=my_tool,
        )

        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        call_count = {"n": 0}

        async def mock_stream(messages, system_prompt, tools, **kw):
            n = call_count["n"]
            call_count["n"] += 1
            if n == 0:
                yield {
                    "type": "tool_call_delta",
                    "index": 0,
                    "id": "tc_1",
                    "function": {
                        "name": "capture",
                        "arguments": json.dumps({"prompt": "x"}),
                    },
                }
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
            else:
                yield {"type": "text", "text": "ok"}
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.session._sessions_dir", return_value=tmp_path / "sessions"),
        ):
            async for _ in backend.stream(
                "go",
                tools=[td],
                session_id="sess1",
            ):
                pass

        assert captured["context"] is not None
        assert "turn_id" in captured["context"]
        assert "session_id" in captured["context"]
        assert captured["context"]["session_id"] == "sess1"


# ---------------------------------------------------------------------------
# Bug 6: _session_modes dict must be bounded
# ---------------------------------------------------------------------------


class TestSessionModesBoundedMemory:
    def test_session_modes_bounded_memory(self, tmp_path):
        """Long-running parents that spawn many child sessions should not
        leak memory through the _session_modes dict. The dict is capped
        at MAX_SESSION_MODES_ENTRIES with FIFO eviction.
        """
        from aloop.agent_backend import ALoop, MAX_SESSION_MODES_ENTRIES

        backend = ALoop(model="minimax-m2.5", api_key="test-key")
        # Insert way more than the cap.
        for i in range(MAX_SESSION_MODES_ENTRIES + 500):
            backend._record_session_mode(f"session_{i}", "test_mode")

        assert len(backend._session_modes) == MAX_SESSION_MODES_ENTRIES
        # The oldest entries should be evicted (FIFO).
        assert "session_0" not in backend._session_modes
        # The most recent entries should still be present.
        assert f"session_{MAX_SESSION_MODES_ENTRIES + 499}" in backend._session_modes

    def test_record_session_mode_refreshes_on_repeat(self, tmp_path):
        """Re-recording an existing session_id moves it to the end so
        it's not evicted as 'oldest'."""
        from aloop.agent_backend import ALoop, MAX_SESSION_MODES_ENTRIES

        backend = ALoop(model="minimax-m2.5", api_key="test-key")
        backend._record_session_mode("alpha", "mode_a")
        for i in range(MAX_SESSION_MODES_ENTRIES - 1):
            backend._record_session_mode(f"filler_{i}", "mode_b")
        # alpha is currently the oldest entry. Refresh it.
        backend._record_session_mode("alpha", "mode_a")
        # Now add one more entry — alpha should NOT be evicted.
        backend._record_session_mode("z", "mode_z")
        assert "alpha" in backend._session_modes
        assert len(backend._session_modes) == MAX_SESSION_MODES_ENTRIES


# ---------------------------------------------------------------------------
# Issue 7: fork-from-fork end-to-end
# ---------------------------------------------------------------------------


class TestForkFromForkRecursive:
    @pytest.mark.asyncio
    async def test_fork_from_fork_recursive(self, tmp_path):
        """Recursive forking: parent forks → child, child forks → grandchild.

        Verify:
        - Child has fork_from = parent
        - Grandchild has fork_from = child
        - Both have spawn_metadata with kind="fork"
        - The child's _session_modes propagation lets the grandchild's
          spawn validation work (the child's mode label is inherited from
          the parent so the grandchild can see can_fork on the same mode).
        """
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        config = {
            "modes": {
                "orch": {
                    "can_fork": True,
                    "tools": ["read_file"],
                },
            }
        }

        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        # Track how many turns each context (parent/child/grandchild) has
        # taken so we can return text after the first tool-call turn for
        # the child too. This avoids the child spinning until max iters.
        state = {
            "parent_turns": 0,
            "child_turns": 0,
            "grandchild_turns": 0,
        }

        async def mock_stream(messages, system_prompt, tools, **kw):
            from aloop.agent_result import FORK_BOILERPLATE
            last_user = next(
                (m for m in reversed(messages) if m.get("role") == "user"), None
            )
            content = (last_user or {}).get("content", "") or ""
            is_subagent = isinstance(content, str) and content.startswith(
                FORK_BOILERPLATE
            )
            is_grandchild = is_subagent and "GRANDCHILD_DIRECTIVE" in content
            is_child = is_subagent and not is_grandchild

            if is_grandchild:
                state["grandchild_turns"] += 1
                yield {"type": "text", "text": "grandchild done"}
                yield {"type": "usage", "usage": {"prompt_tokens": 3, "completion_tokens": 3}}
                return

            if is_child:
                state["child_turns"] += 1
                if state["child_turns"] == 1:
                    # First child turn — spawn the grandchild via fork
                    yield {
                        "type": "tool_call_delta",
                        "index": 0,
                        "id": "tc_grand",
                        "function": {
                            "name": "agent",
                            "arguments": json.dumps({
                                "prompt": "GRANDCHILD_DIRECTIVE explore deeper",
                                "description": "deeper",
                            }),
                        },
                    }
                    yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
                else:
                    # Second child turn — return text so the child loop ends
                    yield {"type": "text", "text": "child summary with grandchild result"}
                    yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
                return

            # Parent: first call emits tool call, subsequent emits text
            state["parent_turns"] += 1
            if state["parent_turns"] == 1:
                yield {
                    "type": "tool_call_delta",
                    "index": 0,
                    "id": "tc_child",
                    "function": {
                        "name": "agent",
                        "arguments": json.dumps({
                            "prompt": "explore for me",
                            "description": "explore",
                        }),
                    },
                }
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
            else:
                yield {"type": "text", "text": "parent summary"}
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 3}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.session._sessions_dir", return_value=sessions_dir),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            async for _ in backend.stream("start", mode="orch", session_id="grandparent"):
                pass

        # Find all sessions
        ctx_files = list(sessions_dir.glob("*.context.json"))
        sessions_by_fork_from: dict[str | None, list[dict]] = {}
        for f in ctx_files:
            data = json.loads(f.read_text())
            sessions_by_fork_from.setdefault(data.get("fork_from"), []).append(data)

        # Original parent has fork_from=None
        assert "grandparent" in [s.get("session_id") for s in sessions_by_fork_from.get(None, [])]

        # Find direct children of grandparent
        children = sessions_by_fork_from.get("grandparent", [])
        assert len(children) == 1, f"expected 1 direct child, got {len(children)}"
        child = children[0]
        assert child["spawn_metadata"]["kind"] == "fork"
        assert child["spawn_metadata"]["parent_session_id"] == "grandparent"
        # Mode label propagated through fork
        assert child["spawn_metadata"]["spawning_mode"] == "orch"

        # Find grandchildren (children of child)
        child_id = child["session_id"]
        grandchildren = sessions_by_fork_from.get(child_id, [])
        assert len(grandchildren) == 1, (
            f"expected 1 grandchild forked from child {child_id}, got {len(grandchildren)}"
        )
        grandchild = grandchildren[0]
        assert grandchild["spawn_metadata"]["kind"] == "fork"
        assert grandchild["spawn_metadata"]["parent_session_id"] == child_id
        assert grandchild["spawn_metadata"]["spawning_mode"] == "orch"


# ---------------------------------------------------------------------------
# Issue 8: fork child inherits parent mode label via _session_modes
# ---------------------------------------------------------------------------


class TestForkChildInheritsParentModeLabel:
    @pytest.mark.asyncio
    async def test_fork_child_inherits_parent_mode_label(self, tmp_path):
        """After a fork spawn, the parent_loop._session_modes dict should
        record the child session under the parent's mode name. This is
        what enables fork-from-fork to find the same spawnable_modes
        config on recursive spawns.
        """
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        config = {"modes": {"orch": {"can_fork": True, "tools": ["read_file"]}}}
        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        call_count = {"n": 0}

        async def mock_stream(messages, system_prompt, tools, **kw):
            from aloop.agent_result import FORK_BOILERPLATE
            n = call_count["n"]
            call_count["n"] += 1
            last_user = next(
                (m for m in reversed(messages) if m.get("role") == "user"), None
            )
            content = (last_user or {}).get("content", "") or ""
            is_child = isinstance(content, str) and content.startswith(
                FORK_BOILERPLATE
            )
            if is_child:
                yield {"type": "text", "text": "child"}
                yield {"type": "usage", "usage": {"prompt_tokens": 3, "completion_tokens": 3}}
                return
            if n == 0:
                yield {
                    "type": "tool_call_delta",
                    "index": 0,
                    "id": "tc_1",
                    "function": {
                        "name": "agent",
                        "arguments": json.dumps({
                            "prompt": "explore",
                            "description": "x",
                        }),
                    },
                }
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
            else:
                yield {"type": "text", "text": "ok"}
                yield {"type": "usage", "usage": {"prompt_tokens": 5, "completion_tokens": 3}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.session._sessions_dir", return_value=sessions_dir),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            async for _ in backend.stream("start", mode="orch", session_id="parent_inherit"):
                pass

        # The parent and the fork child should both be tracked in
        # _session_modes under the "orch" mode label.
        assert backend._session_modes.get("parent_inherit") == "orch"
        # Find the child session
        ctx_files = list(sessions_dir.glob("*.context.json"))
        child_ids = []
        for f in ctx_files:
            data = json.loads(f.read_text())
            if data.get("fork_from") == "parent_inherit":
                child_ids.append(data["session_id"])
        assert len(child_ids) == 1
        child_id = child_ids[0]
        assert backend._session_modes.get(child_id) == "orch", (
            f"expected child {child_id} to inherit 'orch' mode label from parent"
        )
