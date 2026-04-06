"""AgentResult dataclass, fork boilerplate, and result extraction.

These primitives are used by the subagent system (executor + agent tool)
to represent the output of a spawned child agent and to wrap fork-path
prompts with a directive that turns the inherited context into a worker
brief.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


FORK_BOILERPLATE = """\
<subagent_directive>
You are a forked subagent worker. The conversation history above is your
parent agent's context — you inherited it for cache efficiency. Your job
is to execute the directive below directly and concisely, then return.

Rules:
- Execute the directive below. Do not converse, ask clarifying questions,
  or editorialize. The parent already decided what it needs.
- Stay strictly within the directive's scope. Do not expand the task.
- Your final assistant text response IS your report to the parent. Keep
  it focused and structured. Lead with the answer.
- Suggested format: Scope -> Result -> Key files -> Issues. Adapt as
  appropriate. Aim for under 500 words unless the directive explicitly
  asks for more detail.
- If you need to produce substantial output (long code, large analyses),
  write it to a file and reference the path in your summary.
- You may spawn further subagents if the directive genuinely requires it.
  Do not spawn agents to avoid doing simple work yourself.
- If you make file changes, commit them before reporting (when in a git
  repo). Mention the commit in your summary.

Directive:
</subagent_directive>

"""


@dataclass
class AgentResult:
    """Result from a spawned subagent run.

    Attributes:
        text: the child's final assistant text — what gets returned to the
            parent. Empty string if the child produced no text.
        session_id: the child session's id. Always set when the child
            actually ran (may be empty if spawn failed before LOOP_START).
        spawn_kind: 'fork' (inherited parent context via session forking)
            or 'fresh' (clean session, configured by mode).
        mode: the child's mode name. None for fork-path children (they
            inherit the parent's mode label, see executor).
        parent_session_id: parent session id at spawn time, if any.
        parent_turn_id: parent's current turn_id at spawn time, if any.
        usage: dict with input_tokens, output_tokens, cost_usd, model, turns.
    """

    text: str
    session_id: str
    spawn_kind: Literal["fork", "fresh"]
    mode: str | None = None
    parent_session_id: str | None = None
    parent_turn_id: str | None = None
    usage: dict = field(default_factory=dict)


def extract_partial_result(messages: list[dict]) -> str:
    """Walk backwards through messages, extract the last assistant text.

    Mirrors Claude Code's extractPartialResult: returns the most recent
    non-empty assistant text content. If no such message exists (e.g. the
    child only emitted tool calls and ran out of iterations), returns the
    empty string.

    Handles both string content and block-list content (Anthropic-style).
    """
    if not messages:
        return ""

    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            text_parts = [
                str(b.get("text", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            joined = "".join(text_parts).strip()
            if joined:
                return joined
    return ""
