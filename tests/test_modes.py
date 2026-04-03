"""Tests for named mode configuration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aloop.config import load_mode, resolve_mode_system_prompt
from aloop.types import ModeConflictError


# ---------------------------------------------------------------------------
# load_mode
# ---------------------------------------------------------------------------


class TestLoadMode:
    def test_load_valid_mode(self):
        config = {
            "modes": {
                "default": {"system_prompt": "You are helpful.", "model": "gpt-4o"},
                "review": {"system_prompt": "You are a reviewer.", "tools": ["read_file"]},
            }
        }
        mode = load_mode("review", config)
        assert mode["system_prompt"] == "You are a reviewer."
        assert mode["tools"] == ["read_file"]

    def test_load_invalid_mode_raises(self):
        config = {
            "modes": {
                "default": {"system_prompt": "hi"},
            }
        }
        with pytest.raises(ValueError, match="Unknown mode: 'review'"):
            load_mode("review", config)

    def test_load_mode_lists_available(self):
        config = {
            "modes": {
                "default": {},
                "fast": {},
                "review": {},
            }
        }
        with pytest.raises(ValueError, match="Available: "):
            load_mode("nonexistent", config)

    def test_load_mode_no_modes_section(self):
        config = {}
        with pytest.raises(ValueError, match="Unknown mode"):
            load_mode("anything", config)

    def test_load_mode_returns_copy(self):
        """Returned dict is a copy — modifying it doesn't affect the config."""
        inner = {"system_prompt": "original"}
        config = {"modes": {"default": inner}}
        result = load_mode("default", config)
        result["system_prompt"] = "mutated"
        assert inner["system_prompt"] == "original"


# ---------------------------------------------------------------------------
# resolve_mode_system_prompt
# ---------------------------------------------------------------------------


class TestResolveModeSystemPrompt:
    def test_inline_system_prompt(self):
        mode = {"system_prompt": "You are a helpful assistant."}
        assert resolve_mode_system_prompt(mode) == "You are a helpful assistant."

    def test_system_prompt_file(self, tmp_path):
        prompt_file = tmp_path / ".aloop" / "prompts" / "reviewer.md"
        prompt_file.parent.mkdir(parents=True)
        prompt_file.write_text("Review code carefully.", encoding="utf-8")

        mode = {"system_prompt_file": ".aloop/prompts/reviewer.md"}
        result = resolve_mode_system_prompt(mode, project_root=tmp_path)
        assert result == "Review code carefully."

    def test_system_prompt_file_missing(self, tmp_path):
        mode = {"system_prompt_file": ".aloop/prompts/nonexistent.md"}
        result = resolve_mode_system_prompt(mode, project_root=tmp_path)
        assert result is None

    def test_neither_set(self):
        mode = {"tools": ["read_file"]}
        assert resolve_mode_system_prompt(mode) is None

    def test_system_prompt_takes_priority_over_file(self):
        """If both system_prompt and system_prompt_file are set, system_prompt wins."""
        mode = {
            "system_prompt": "Inline wins.",
            "system_prompt_file": ".aloop/prompts/file.md",
        }
        assert resolve_mode_system_prompt(mode) == "Inline wins."


# ---------------------------------------------------------------------------
# Mode application in stream()
# ---------------------------------------------------------------------------


class TestModeInStream:
    @pytest.mark.asyncio
    async def test_mode_applies_system_prompt(self, tmp_path):
        """Mode's system_prompt is used when no explicit system_prompt is passed."""
        from aloop.agent_backend import ALoop
        from aloop.types import EventType

        config = {
            "modes": {
                "review": {"system_prompt": "You are a code reviewer."},
            }
        }

        backend = ALoop(model="minimax-m2.5", api_key="test-key")
        captured_system_prompt = None

        async def mock_stream(messages, system_prompt, tools, **kw):
            nonlocal captured_system_prompt
            captured_system_prompt = system_prompt
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            events = []
            async for event in backend.stream("review this", mode="review"):
                events.append(event)

        assert captured_system_prompt == "You are a code reviewer."

    @pytest.mark.asyncio
    async def test_explicit_system_prompt_overrides_mode(self, tmp_path):
        """Explicit system_prompt kwarg overrides mode's system_prompt."""
        from aloop.agent_backend import ALoop
        from aloop.types import EventType

        config = {
            "modes": {
                "review": {"system_prompt": "Mode prompt."},
            }
        }

        backend = ALoop(model="minimax-m2.5", api_key="test-key")
        captured_system_prompt = None

        async def mock_stream(messages, system_prompt, tools, **kw):
            nonlocal captured_system_prompt
            captured_system_prompt = system_prompt
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            events = []
            async for event in backend.stream(
                "review this",
                mode="review",
                system_prompt="Explicit override.",
            ):
                events.append(event)

        assert captured_system_prompt == "Explicit override."

    @pytest.mark.asyncio
    async def test_mode_applies_tools_filter(self, tmp_path):
        """Mode's tools list filters available tools by name."""
        from aloop.agent_backend import ALoop
        from aloop.types import EventType

        config = {
            "modes": {
                "readonly": {"tools": ["read_file"]},
            }
        }

        backend = ALoop(model="minimax-m2.5", api_key="test-key")
        captured_tools = None

        async def mock_stream(messages, system_prompt, tools, **kw):
            nonlocal captured_tools
            captured_tools = tools
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            events = []
            async for event in backend.stream("read something", mode="readonly"):
                events.append(event)

        # captured_tools is the tool_schemas list passed to _stream_completion
        # It should only contain schemas for tools whose names are in the mode's list
        assert captured_tools is not None
        tool_names = [t["function"]["name"] for t in captured_tools]
        assert "read_file" in tool_names
        # Other tools like write_file, edit_file, bash should be excluded
        assert "write_file" not in tool_names
        assert "bash" not in tool_names

    @pytest.mark.asyncio
    async def test_mode_applies_model(self, tmp_path):
        """Mode's model overrides the constructor default."""
        from aloop.agent_backend import ALoop
        from aloop.types import EventType

        config = {
            "modes": {
                "fast": {"model": "x-ai/grok-4.1-fast"},
            }
        }

        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        async def mock_stream(messages, system_prompt, tools, **kw):
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            events = []
            async for event in backend.stream("test", mode="fast"):
                events.append(event)

        # After stream, the model_config should reflect the mode's model
        loop_start = next(e for e in events if e.type == EventType.LOOP_START)
        assert loop_start.data["model"] == "x-ai/grok-4.1-fast"

    @pytest.mark.asyncio
    async def test_mode_applies_max_iterations(self, tmp_path):
        """Mode's max_iterations overrides the constructor default."""
        from aloop.agent_backend import ALoop

        config = {
            "modes": {
                "limited": {"max_iterations": 5},
            }
        }

        backend = ALoop(model="minimax-m2.5", api_key="test-key")
        assert backend.max_iterations == 50  # default

        async def mock_stream(messages, system_prompt, tools, **kw):
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            events = []
            async for event in backend.stream("test", mode="limited"):
                events.append(event)

        # During the stream, max_iterations was 5
        assert backend.max_iterations == 5

    @pytest.mark.asyncio
    async def test_mode_applies_compaction(self, tmp_path):
        """Mode's compaction settings override defaults."""
        from aloop.agent_backend import ALoop

        config = {
            "modes": {
                "compact": {
                    "compaction": {
                        "reserve_tokens": 8192,
                        "keep_recent_tokens": 10000,
                    }
                },
            }
        }

        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        async def mock_stream(messages, system_prompt, tools, **kw):
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            events = []
            async for event in backend.stream("test", mode="compact"):
                events.append(event)

        assert backend.compaction_settings.reserve_tokens == 8192
        assert backend.compaction_settings.keep_recent_tokens == 10000

    @pytest.mark.asyncio
    async def test_mode_with_system_prompt_file(self, tmp_path):
        """Mode with system_prompt_file reads from disk."""
        from aloop.agent_backend import ALoop

        prompt_dir = tmp_path / ".aloop" / "prompts"
        prompt_dir.mkdir(parents=True)
        (prompt_dir / "reviewer.md").write_text("Review code now.", encoding="utf-8")

        config = {
            "modes": {
                "review": {"system_prompt_file": ".aloop/prompts/reviewer.md"},
            }
        }

        backend = ALoop(model="minimax-m2.5", api_key="test-key")
        captured_system_prompt = None

        async def mock_stream(messages, system_prompt, tools, **kw):
            nonlocal captured_system_prompt
            captured_system_prompt = system_prompt
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            events = []
            async for event in backend.stream("review this", mode="review"):
                events.append(event)

        assert captured_system_prompt == "Review code now."

    @pytest.mark.asyncio
    async def test_no_mode_uses_defaults(self, tmp_path):
        """Without mode, constructor defaults apply."""
        from aloop.agent_backend import ALoop
        from aloop.types import EventType

        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        async def mock_stream(messages, system_prompt, tools, **kw):
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with patch.object(backend, "_stream_completion", side_effect=mock_stream):
            events = []
            async for event in backend.stream("test"):
                events.append(event)

        loop_start = next(e for e in events if e.type == EventType.LOOP_START)
        assert loop_start.data["model"] == "minimax-m2.5"


# ---------------------------------------------------------------------------
# ModeConflictError
# ---------------------------------------------------------------------------


class TestModeConflictError:
    @pytest.mark.asyncio
    async def test_same_session_different_mode_raises(self, tmp_path):
        """Calling stream() with a different mode on the same session raises."""
        from aloop.agent_backend import ALoop

        config = {
            "modes": {
                "review": {"system_prompt": "Review."},
                "code": {"system_prompt": "Code."},
            }
        }

        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        async def mock_stream(messages, system_prompt, tools, **kw):
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            # First call — sets mode to "review"
            async for _ in backend.stream("test", session_id="s1", mode="review"):
                pass

            # Second call — same session, different mode
            with pytest.raises(ModeConflictError, match="mode 'review'"):
                async for _ in backend.stream("test", session_id="s1", mode="code"):
                    pass

    @pytest.mark.asyncio
    async def test_same_session_same_mode_ok(self, tmp_path):
        """Calling stream() with the same mode on the same session is fine."""
        from aloop.agent_backend import ALoop

        config = {
            "modes": {
                "review": {"system_prompt": "Review."},
            }
        }

        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        async def mock_stream(messages, system_prompt, tools, **kw):
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            # Both calls with same mode — should not raise
            async for _ in backend.stream("test1", session_id="s1", mode="review"):
                pass
            async for _ in backend.stream("test2", session_id="s1", mode="review"):
                pass

    @pytest.mark.asyncio
    async def test_different_sessions_different_modes_ok(self, tmp_path):
        """Different sessions can use different modes."""
        from aloop.agent_backend import ALoop

        config = {
            "modes": {
                "review": {"system_prompt": "Review."},
                "code": {"system_prompt": "Code."},
            }
        }

        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        async def mock_stream(messages, system_prompt, tools, **kw):
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            async for _ in backend.stream("test", session_id="s1", mode="review"):
                pass
            async for _ in backend.stream("test", session_id="s2", mode="code"):
                pass

    @pytest.mark.asyncio
    async def test_no_session_id_no_conflict_check(self, tmp_path):
        """Without session_id, no mode conflict is possible."""
        from aloop.agent_backend import ALoop

        config = {
            "modes": {
                "review": {"system_prompt": "Review."},
                "code": {"system_prompt": "Code."},
            }
        }

        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        async def mock_stream(messages, system_prompt, tools, **kw):
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            # No session_id — both should work
            async for _ in backend.stream("test", mode="review"):
                pass
            async for _ in backend.stream("test", mode="code"):
                pass


# ---------------------------------------------------------------------------
# ACP set_session_mode
# ---------------------------------------------------------------------------


class TestAcpSetSessionMode:
    @pytest.mark.asyncio
    async def test_set_session_mode_updates_state(self, tmp_path):
        """set_session_mode updates the session's mode name."""
        from aloop.acp import AloopAgent

        config = {
            "modes": {
                "review": {
                    "system_prompt": "You are a reviewer.",
                    "model": "x-ai/grok-4.1-fast",
                },
            }
        }

        agent = AloopAgent(model="minimax-m2.5")
        mock_conn = AsyncMock()
        agent.on_connect(mock_conn)

        resp = await agent.new_session(cwd=str(tmp_path))
        sid = resp.session_id
        state = agent._sessions[sid]
        assert state.mode is None

        with (
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            await agent.set_session_mode(mode_id="review", session_id=sid)

        state = agent._sessions[sid]
        assert state.mode == "review"
        assert state.backend.model_config.id == "x-ai/grok-4.1-fast"

    @pytest.mark.asyncio
    async def test_set_session_mode_unknown_session(self):
        """set_session_mode on unknown session is a no-op."""
        from aloop.acp import AloopAgent

        agent = AloopAgent(model="minimax-m2.5")
        mock_conn = AsyncMock()
        agent.on_connect(mock_conn)

        # Should not raise
        result = await agent.set_session_mode(mode_id="review", session_id="nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_session_mode_invalid_mode_raises(self, tmp_path):
        """set_session_mode with invalid mode raises ValueError."""
        from aloop.acp import AloopAgent

        config = {"modes": {"review": {"system_prompt": "hi"}}}

        agent = AloopAgent(model="minimax-m2.5")
        mock_conn = AsyncMock()
        agent.on_connect(mock_conn)

        resp = await agent.new_session(cwd=str(tmp_path))
        sid = resp.session_id

        with (
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            with pytest.raises(ValueError, match="Unknown mode"):
                await agent.set_session_mode(mode_id="nonexistent", session_id=sid)

    @pytest.mark.asyncio
    async def test_set_session_mode_with_max_iterations(self, tmp_path):
        """set_session_mode applies max_iterations from mode config."""
        from aloop.acp import AloopAgent

        config = {
            "modes": {
                "limited": {"max_iterations": 10},
            }
        }

        agent = AloopAgent(model="minimax-m2.5")
        mock_conn = AsyncMock()
        agent.on_connect(mock_conn)

        resp = await agent.new_session(cwd=str(tmp_path))
        sid = resp.session_id

        with (
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            await agent.set_session_mode(mode_id="limited", session_id=sid)

        state = agent._sessions[sid]
        assert state.backend.max_iterations == 10


# ---------------------------------------------------------------------------
# CLI --mode flag
# ---------------------------------------------------------------------------


class TestCliMode:
    def test_mode_flag_parsed(self):
        from aloop.cli import parse_args

        args = parse_args(["run", "--mode", "review", "test prompt"])
        assert args.mode == "review"

    def test_mode_flag_none_by_default(self):
        from aloop.cli import parse_args

        args = parse_args(["run", "test prompt"])
        assert args.mode is None

    def test_mode_flag_short_form_not_available(self):
        """--mode has no short form — verify it doesn't conflict with -m (model)."""
        from aloop.cli import parse_args

        args = parse_args(["run", "-m", "gpt-4o", "test prompt"])
        assert args.model == "gpt-4o"
        assert args.mode is None


# ---------------------------------------------------------------------------
# Mode precedence: explicit kwargs > mode > defaults
# ---------------------------------------------------------------------------


class TestModePrecedence:
    @pytest.mark.asyncio
    async def test_defaults_restored_after_mode(self, tmp_path):
        """After a mode stream call, calling without mode uses constructor defaults."""
        from aloop.agent_backend import ALoop
        from aloop.types import EventType

        config = {
            "modes": {
                "fast": {"model": "x-ai/grok-4.1-fast", "max_iterations": 5},
            }
        }

        backend = ALoop(model="minimax-m2.5", api_key="test-key")

        async def mock_stream(messages, system_prompt, tools, **kw):
            yield {"type": "text", "text": "ok"}
            yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        with (
            patch.object(backend, "_stream_completion", side_effect=mock_stream),
            patch("aloop.system_prompt._load_aloop_config", return_value=config),
            patch("aloop.get_project_root", return_value=tmp_path),
        ):
            # First: use mode
            events1 = []
            async for event in backend.stream("test", mode="fast"):
                events1.append(event)

            # Model should be the mode's
            loop_start1 = next(e for e in events1 if e.type == EventType.LOOP_START)
            assert loop_start1.data["model"] == "x-ai/grok-4.1-fast"

        # Second: no mode — should revert to defaults
        with patch.object(backend, "_stream_completion", side_effect=mock_stream):
            events2 = []
            async for event in backend.stream("test2"):
                events2.append(event)

            loop_start2 = next(e for e in events2 if e.type == EventType.LOOP_START)
            assert loop_start2.data["model"] == "minimax-m2.5"
            assert backend.max_iterations == 50
