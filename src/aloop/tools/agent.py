"""The 'agent' built-in tool — delegate work to a subagent.

This module exports a factory ``build_agent_tool`` rather than a bare
ToolDef because the tool's description is dynamic — it depends on which
modes are spawnable from the *current* mode. The backend constructs a
fresh ToolDef per stream call when a mode opts in via ``spawnable_modes``
or ``can_fork``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..agent_result import AgentResult
from ..tools_base import ToolDef, ToolResult

if TYPE_CHECKING:
    from ..agent_backend import ALoop
    from ..executor import AgentExecutor


_AGENT_TOOL_DESCRIPTION_TEMPLATE = """\
Delegate a task to a subagent. Two modes:

FORK (omit `mode`): The subagent inherits your full conversation history
at the current point and runs from there. Use this for parallel
exploration or read-only work where context is needed but you don't
want to keep the intermediate output. Forks share your prompt cache —
they're cheap. Don't override `model` on forks (breaks the cache).

FRESH (specify `mode`): The subagent gets a clean session with the
specified mode's configuration: its own model, system prompt, tool set,
and permissions. Use for focused subtasks that need scoped tools or a
different model. Fresh agents start with ZERO context — your `prompt`
must brief them fully. Never delegate understanding ("based on your
findings, fix it" is an anti-pattern).

The subagent's final assistant text response is returned to you as the
result. For substantial outputs, instruct the subagent to write to files
and reference the paths in its summary.

Available subagent modes from your current mode:
{mode_listing}

Guidance:
- Brief fresh agents fully — they have no context.
- Don't peek at child output mid-flight; wait for the result.
- Multiple agent tool calls in one turn are run sequentially in v0.6.0.
- The `description` field (3-5 words) helps with traceability.
"""


def _format_mode_listing(spawnable: list[str], all_modes: dict[str, dict]) -> str:
    """Format the spawnable modes block for the tool description.

    For each spawnable mode, include its tool list and the first line of
    its system prompt (if any) as a brief one-line summary.
    """
    if not spawnable:
        return "  (none — only fork mode is available)"

    lines: list[str] = []
    for name in spawnable:
        mode_cfg = all_modes.get(name, {}) if isinstance(all_modes, dict) else {}
        if not isinstance(mode_cfg, dict):
            mode_cfg = {}

        tools = mode_cfg.get("tools", [])
        if tools == ["*"]:
            tools_str = "all tools"
        elif isinstance(tools, list) and tools:
            tools_str = ", ".join(str(t) for t in tools)
        else:
            tools_str = "default tools"

        sp = mode_cfg.get("system_prompt") or mode_cfg.get("system_prompt_file") or ""
        if sp:
            first_line = str(sp).splitlines()[0][:80]
        else:
            first_line = "(no description)"
        lines.append(f"- {name}: {first_line} (tools: {tools_str})")
    return "\n".join(lines)


def build_agent_tool(
    *,
    spawnable_modes: list[str],
    can_fork: bool,
    all_modes: dict[str, dict],
    parent_loop: "ALoop",
    executor: "AgentExecutor",
    current_mode_name: str | None = None,
) -> ToolDef:
    """Construct an agent tool ToolDef for a specific mode context.

    The description is computed dynamically from the spawnable_modes
    available to the current mode. The execute closure captures
    parent_loop and executor so it can spawn children with the right
    machinery.
    """
    description = _AGENT_TOOL_DESCRIPTION_TEMPLATE.format(
        mode_listing=_format_mode_listing(spawnable_modes, all_modes),
    )
    if current_mode_name:
        description += f"\n\nYou are currently in mode `{current_mode_name}`."
    if not can_fork:
        description += (
            "\n\nNOTE: The fork path (omitting `mode`) is disabled for your "
            "current mode. You must specify a `mode`."
        )

    spawnable_set = set(spawnable_modes)

    async def _spawn_agent(
        prompt: str,
        description: str,
        mode: str | None = None,
        model: str | None = None,
        _context: dict | None = None,
    ) -> ToolResult:
        ctx = _context or {}
        parent_session_id = ctx.get("session_id")
        parent_turn_id = ctx.get("turn_id")

        if mode is None:
            if not can_fork:
                return ToolResult(
                    content=(
                        "agent: fork-path spawning (no mode specified) is not "
                        "allowed from the current mode. Specify a `mode`."
                    ),
                    is_error=True,
                )
            if parent_session_id is None or parent_turn_id is None:
                return ToolResult(
                    content=(
                        "agent: fork-path spawning requires a persistent parent "
                        "session. The parent must be running with "
                        "persist_session=True."
                    ),
                    is_error=True,
                )
            fork = True
        else:
            if mode not in spawnable_set:
                allowed = ", ".join(spawnable_modes) if spawnable_modes else "(none)"
                return ToolResult(
                    content=(
                        f"agent: mode {mode!r} is not in the allowed "
                        f"spawnable_modes for the current mode. Allowed: {allowed}"
                    ),
                    is_error=True,
                )
            target_cfg = all_modes.get(mode, {}) if isinstance(all_modes, dict) else {}
            if not isinstance(target_cfg, dict):
                target_cfg = {}
            if mode not in (all_modes or {}):
                return ToolResult(
                    content=f"agent: mode {mode!r} is not defined in project config.",
                    is_error=True,
                )
            if not target_cfg.get("subagent_eligible", False):
                return ToolResult(
                    content=(
                        f"agent: mode {mode!r} is not subagent_eligible. "
                        f"Set 'subagent_eligible: true' on the mode to allow it."
                    ),
                    is_error=True,
                )
            fork = False

        try:
            handle = await executor.spawn(
                prompt=prompt,
                mode=mode,
                model=model,
                parent_session_id=parent_session_id,
                parent_turn_id=parent_turn_id,
                fork=fork,
                parent_loop=parent_loop,
            )
            agent_result: AgentResult = await handle.result()
        except Exception as exc:
            return ToolResult(
                content=f"agent: spawn failed: {exc}",
                is_error=True,
            )

        body_lines = [agent_result.text or "(child produced no text)"]
        body_lines.append("")
        body_lines.append(
            f"[child session: {agent_result.session_id or '(unknown)'} | "
            f"kind: {agent_result.spawn_kind} | "
            f"mode: {agent_result.mode or '(inherited)'} | "
            f"in: {agent_result.usage.get('input_tokens', 0)} "
            f"out: {agent_result.usage.get('output_tokens', 0)} "
            f"turns: {agent_result.usage.get('turns', 0)}]"
        )
        return ToolResult(
            content="\n".join(body_lines),
            details={
                "session_id": agent_result.session_id,
                "spawn_kind": agent_result.spawn_kind,
                "mode": agent_result.mode,
                "usage": agent_result.usage,
            },
        )

    return ToolDef(
        name="agent",
        description=description,
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "Task for the subagent. For fresh agents, must be "
                        "fully self-contained. For forks, the subagent has "
                        "your context — write a directive."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "3-5 word task summary for traceability.",
                },
                "mode": {
                    "type": "string",
                    "description": (
                        "Subagent mode name. Omit to fork at the current "
                        "turn (inherits parent context)."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Optional model override. Leave unset on forks "
                        "to preserve cache."
                    ),
                },
            },
            "required": ["prompt", "description"],
        },
        execute=_spawn_agent,
    )
