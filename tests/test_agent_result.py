"""Tests for AgentResult, FORK_BOILERPLATE, and extract_partial_result."""

from __future__ import annotations

from aloop.agent_result import (
    AgentResult,
    FORK_BOILERPLATE,
    extract_partial_result,
)


# ---------------------------------------------------------------------------
# extract_partial_result
# ---------------------------------------------------------------------------


class TestExtractPartialResult:
    def test_simple_text(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello there"},
        ]
        assert extract_partial_result(msgs) == "hello there"

    def test_walks_backwards(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "first"},
            {"role": "user", "content": "more"},
            {"role": "assistant", "content": "second"},
        ]
        assert extract_partial_result(msgs) == "second"

    def test_skips_empty(self):
        msgs = [
            {"role": "assistant", "content": "earlier"},
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": "   "},
        ]
        assert extract_partial_result(msgs) == "earlier"

    def test_handles_block_list_content(self):
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "block one "},
                    {"type": "text", "text": "block two"},
                ],
            },
        ]
        assert extract_partial_result(msgs) == "block one block two"

    def test_skips_tool_only_assistant(self):
        msgs = [
            {"role": "assistant", "content": "earlier text"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "tc1", "function": {"name": "x", "arguments": "{}"}}],
            },
        ]
        assert extract_partial_result(msgs) == "earlier text"

    def test_no_assistant(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": "result", "tool_call_id": "tc1"},
        ]
        assert extract_partial_result(msgs) == ""

    def test_empty_message_list(self):
        assert extract_partial_result([]) == ""

    def test_strips_whitespace_only_content(self):
        msgs = [
            {"role": "assistant", "content": "real text"},
            {"role": "assistant", "content": "\n\n  \t"},
        ]
        assert extract_partial_result(msgs) == "real text"

    def test_block_list_skipped_when_only_non_text_blocks(self):
        msgs = [
            {"role": "assistant", "content": "earlier"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "foo", "input": {}}],
            },
        ]
        assert extract_partial_result(msgs) == "earlier"


# ---------------------------------------------------------------------------
# FORK_BOILERPLATE constant
# ---------------------------------------------------------------------------


class TestForkBoilerplate:
    def test_constant_present(self):
        assert isinstance(FORK_BOILERPLATE, str)
        assert "subagent_directive" in FORK_BOILERPLATE
        assert "Directive:" in FORK_BOILERPLATE
        # Should end with a separator so the directive text appears clean
        assert FORK_BOILERPLATE.endswith("\n\n")

    def test_constant_includes_rules(self):
        assert "Rules:" in FORK_BOILERPLATE


# ---------------------------------------------------------------------------
# AgentResult dataclass
# ---------------------------------------------------------------------------


class TestAgentResultDataclass:
    def test_defaults(self):
        result = AgentResult(text="hi", session_id="sid", spawn_kind="fork")
        assert result.text == "hi"
        assert result.session_id == "sid"
        assert result.spawn_kind == "fork"
        assert result.mode is None
        assert result.parent_session_id is None
        assert result.parent_turn_id is None
        assert result.usage == {}

    def test_full_construction(self):
        result = AgentResult(
            text="done",
            session_id="child123",
            spawn_kind="fresh",
            mode="reviewer",
            parent_session_id="parent456",
            parent_turn_id="t789",
            usage={"input_tokens": 10, "output_tokens": 5},
        )
        assert result.mode == "reviewer"
        assert result.usage["input_tokens"] == 10
