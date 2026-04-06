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
