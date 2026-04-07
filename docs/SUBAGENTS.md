# Subagents

aloop subagents let an agent delegate work to a child agent through the built-in `agent` tool. There are two paths: **fork** (the child inherits the parent's full conversation context via session forking) and **fresh** (the child runs a clean session configured by a named mode). Both paths persist lineage metadata and roll up cleanly into the existing session and event model.

Subagents are off by default. A mode opts in by setting `spawnable_modes` (allowlist of modes it can spawn fresh) and/or `can_fork: true` (permission to spawn forks). When opted in, aloop auto-injects the `agent` tool into that mode's tool set.

## Overview

```
Parent agent (mode: orchestrator)
  │
  ├── agent(prompt="explore the auth module", description="auth survey")
  │     └── FORK PATH: child inherits parent context, runs with directive
  │           returns last assistant text → parent sees it as tool result
  │
  └── agent(prompt="...", mode="reviewer", description="review pr 42")
        └── FRESH PATH: clean session, reviewer's model + tools + prompt
              returns last assistant text → parent sees it as tool result
```

| Path | Use when | Cost shape |
|------|----------|-----------|
| **Fork** | You want a worker that sees what you've already gathered. Read-only sweeps, alternate framings, parallel exploration. | Cheap — shares the parent's prompt cache. Don't override `model` or you bust the cache. |
| **Fresh** | You want a focused subtask with scoped tools, a different model, or a different system prompt. | Independent — child has its own session, its own counters, its own model. Brief it fully; it has no prior context. |

The child's **last assistant text block** is returned to the parent as the `agent` tool result. This mirrors Claude Code's `extractPartialResult`. There is no truncation. If the child needs to produce a long output, the agent tool description tells it to write the output to a file and reference the path in its summary.

## Quick example

A minimal orchestrator that can fork worker subagents and spawn a fresh reviewer:

```jsonc
// .aloop/config.json
{
  "modes": {
    "orchestrator": {
      "system_prompt": "You coordinate work. Delegate via the agent tool when useful.",
      "tools": ["read_file", "write_file", "edit_file", "bash", "load_skill"],
      "can_fork": true,
      "spawnable_modes": ["reviewer"]
    },
    "reviewer": {
      "system_prompt": "You review code for correctness and security issues. Read-only.",
      "tools": ["read_file", "grep", "find", "ls"],
      "subagent_eligible": true
    }
  }
}
```

```bash
aloop --mode orchestrator "Add input validation to api/routes/auth.py and have a reviewer check it."
```

The orchestrator will see the `agent` tool in its tool list. It can call it two ways:

```jsonc
// Fork — child inherits orchestrator's conversation
agent(
  prompt="Search the codebase for existing validation helpers we can reuse.",
  description="find validation helpers"
)

// Fresh — child gets the reviewer mode (clean session)
agent(
  prompt="Review the changes in api/routes/auth.py for input validation correctness. Look for SQL injection, missing length checks, and unhandled edge cases. Report findings as a bulleted list.",
  description="security review",
  mode="reviewer"
)
```

## Configuration

Three new mode fields gate subagent behavior. All are optional; omitted means "off".

| Field | Type | Effect |
|-------|------|--------|
| `subagent_eligible` | `bool` | Required for a mode to be a valid spawn target. A mode without `subagent_eligible: true` cannot be named in any other mode's `spawnable_modes`. |
| `spawnable_modes` | `list[string]` | Allowlist of mode names this mode is allowed to spawn via the fresh path. Every entry must exist and must be `subagent_eligible`. Empty list (or omitted) = no fresh-path spawning. |
| `can_fork` | `bool` | If true, this mode can spawn fork-path subagents (omit `mode` on the agent tool call). Independent of `spawnable_modes`. |

A mode is **subagent-enabled** (and gets the `agent` tool injected) if it has either a non-empty `spawnable_modes` or `can_fork: true`. A mode with neither does not see the agent tool.

### Validation

Run `aloop config validate` to check subagent config consistency. The validator (`validate_subagent_config` in `aloop.config`) catches:

- `spawnable_modes` references a mode that doesn't exist
- `spawnable_modes` references a mode that isn't `subagent_eligible`
- `subagent_eligible` is not a bool
- `can_fork` is not a bool
- `spawnable_modes` is not a list of strings

The same validation runs once per `ALoop` instance the first time the agent tool is injected. Errors are logged onto the parent session as `subagent_config_warning` events so users see them at runtime even without running the CLI validator.

## The agent tool

The agent tool is built per stream call by `build_agent_tool` in `aloop/tools/agent.py`. Its description is generated dynamically from the current mode's `spawnable_modes`, so the model sees a tailored tool description listing exactly the modes it can spawn (with each mode's tool list and the first line of its system prompt as a one-line summary).

### Schema

```jsonc
{
  "name": "agent",
  "parameters": {
    "type": "object",
    "properties": {
      "prompt": {
        "type": "string",
        "description": "Task for the subagent. For fresh agents, must be fully self-contained. For forks, the subagent has your context — write a directive."
      },
      "description": {
        "type": "string",
        "description": "3-5 word task summary for traceability."
      },
      "mode": {
        "type": "string",
        "description": "Subagent mode name. Omit to fork at the current turn (inherits parent context)."
      },
      "model": {
        "type": "string",
        "description": "Optional model override. Leave unset on forks to preserve cache."
      }
    },
    "required": ["prompt", "description"]
  }
}
```

### Auto-injection rules

1. After mode resolution, the backend checks the effective mode config for `spawnable_modes` or `can_fork`.
2. If neither is set, no agent tool is added.
3. If either is set, `build_agent_tool` constructs a `ToolDef` with a closure capturing the parent `ALoop` and executor.
4. The tool is appended to the stream's tool list. The active tool whitelist is computed *after* injection so the `agent` name is allowed automatically — you do not need to add `"agent"` to the mode's `tools` list.
5. If a user-supplied tool list already contains a tool named `agent`, injection is skipped (user override wins).
6. For fork-inherited modes (a fork child running under the parent's mode label without an explicit `mode=` kwarg), the backend looks up the mode config from the cached project config so the child also gets the agent tool.

### What the model sees

The auto-generated description includes:

- A guidance preamble explaining fork vs fresh
- A bulleted list of spawnable modes (`name: first-line-of-system-prompt (tools: ...)`)
- The current mode name
- A note if `can_fork` is false (then the model knows it must always specify a `mode`)

The model is instructed to **brief fresh agents fully** — fresh-path children start with zero context, so "based on what you found, fix it" is an anti-pattern.

## Fork path

Fork-path semantics: the child runs with the parent's full conversation history visible. Mechanically, the executor calls `child_loop.stream(prompt=FORK_BOILERPLATE + prompt, fork_from=parent_session_id, fork_at=parent_turn_id)` on the same `ALoop` instance.

### FORK_BOILERPLATE injection

To prevent the fork child from inheriting "I am the orchestrator, I should spawn subagents" behavior from the parent's system prompt, aloop prepends a directive block to the user-supplied prompt. The full text lives in `aloop/agent_result.py` as `FORK_BOILERPLATE`. Key rules it sets:

- You are a forked worker. Execute the directive directly.
- Do not converse, ask clarifying questions, or editorialize.
- Stay strictly within the directive's scope.
- Your final assistant text response IS your report to the parent. Lead with the answer.
- Suggested format: Scope → Result → Key files → Issues. Aim for under 500 words.
- Write substantial output to files; reference paths in the summary.
- You may spawn further subagents if genuinely required.
- Commit file changes before reporting (when in a git repo).

The 500-word target is **behavioral, not enforced**. The full child transcript is always available via the child's session files.

### Persistent parent requirement

Forking requires the parent to have a persistent session (`persist_session=True`, which is the default for any call with a `session_id`). The fork tool reads `parent_session_id` and `parent_turn_id` from `_context` — these are populated by the agent loop when running with persistence. If either is missing, the agent tool returns:

```
agent: fork-path spawning requires a persistent parent session.
The parent must be running with persist_session=True.
```

### Parent state preservation

The fork path reuses the parent's `ALoop` instance. The child stream call would normally clobber per-stream state (model, provider, mode name, session, permissions, token counters, compaction history). The executor snapshots all of this before the spawn and restores it in a `finally` block, so the parent continues with its original config after the child returns.

Token counters are an exception: the executor **adds** the child's usage to the parent's pre-fork tally rather than restoring the snapshot. This way fork-child consumption rolls up into `parent.cost_usd` and `parent.usage` — fork forking is free in terms of context but the tokens still get counted.

### Recursive forking

Forks can spawn forks. The fork child inherits its parent's mode label via `_session_modes` propagation, so a fork child of an `orchestrator` is itself an `orchestrator` and gets the same `agent` tool with the same `spawnable_modes` and `can_fork` config. There is no artificial depth cap.

The only safety valve is the existing **depth-10 auto-materialize** in `AgentSession.resolve_messages()` — if a fork chain reaches depth 10, the deepest session is materialized (parent messages flattened in, parent pointer cleared) before walking further. In practice, subagent depth rarely exceeds 3-4.

## Fresh path

Fresh-path semantics: the child runs with a clean session and the target mode's full configuration — its own model, system prompt, tool set, permissions, and compaction settings.

Mechanically, the executor builds a new `ALoop` instance via `_build_fresh_loop` (reusing the parent's provider, API key, and constructor defaults) and calls `child_loop.stream(prompt=prompt, mode=mode, session_id=child_session_id)`. The child session ID is generated server-side so spawn metadata can be persisted.

### Mode config inheritance

The fresh child inherits **nothing** from the parent's runtime state except the executor's machinery (provider, API key). It inherits **everything** from the target mode's config: model, provider override, system prompt, tools, permissions, max_iterations, compaction settings.

This is by design. A fresh `reviewer` should not see the orchestrator's conversation or model — that defeats the point of running it as a separate scoped agent.

### Self-contained prompts

Fresh agents start with zero prior context. The agent tool description tells the model:

> Brief fresh agents fully — they have no context.

The classic anti-pattern is delegating understanding:

```
// BAD — fresh agent has no idea what "the changes" or "your findings" mean
agent(prompt="based on your findings, fix the issues", mode="worker")

// GOOD — self-contained brief
agent(
  prompt="Read api/routes/auth.py. The login_handler function on line 42 has a SQL injection in the username parameter (no parameterization). Patch it to use sqlalchemy bound parameters. Run `pytest tests/test_auth.py -k login` after editing. Report what you changed and the test result.",
  description="patch sqli",
  mode="worker"
)
```

This is the same convention used by Claude Code and OpenHarness — neither system has a mechanism for the parent to "stream context" into the child.

## Result format

Every spawn produces an `AgentResult` (defined in `aloop/agent_result.py`):

```python
@dataclass
class AgentResult:
    text: str                          # child's final assistant text
    session_id: str                    # child's session id
    spawn_kind: Literal["fork", "fresh"]
    mode: str | None                   # None for fork-path children
    parent_session_id: str | None
    parent_turn_id: str | None
    usage: dict                        # input_tokens, output_tokens, cost_usd, model, turns
```

The `text` field is what the parent sees as the tool result body. It is the child's **last assistant text block** — not the full transcript, not a summary. There is no truncation.

### extract_partial_result fallback

If the child loop ends without producing a final assistant text (e.g. it hit `max_iterations` while still in a tool-call cycle), the executor calls `extract_partial_result(child_session.messages)` to walk backwards through the child's own messages and pull the most recent non-empty assistant text block.

```python
from aloop import extract_partial_result

text = extract_partial_result(messages)  # "" if no assistant text found
```

**Critical detail for fork children:** the fallback uses `child_session.messages` directly, not `resolve_messages()`. For a fork child, `resolve_messages()` walks the parent chain — the most recent assistant text might come from the parent, not the child. Using `messages` directly ensures the fallback never returns parent text as the child's output.

### Lineage footer

The agent tool result body has the child's text followed by a one-line lineage footer:

```
<child's last assistant text>

[child session: 7f3a8b1c | kind: fork | mode: (inherited) | in: 1240 out: 384 turns: 2]
```

Plus a `details` dict on the `ToolResult` with `session_id`, `spawn_kind`, `mode`, and the full `usage` dict for programmatic consumers (event watchers, hooks).

## Spawn metadata

When a child session is created (fork or fresh), the executor writes a `spawn_metadata` dict onto the child's `context.json`:

```json
{
  "spawn_metadata": {
    "kind": "fork",
    "parent_session_id": "a7b3c9d2e1f4",
    "parent_turn_id": "8c2f1d4b6a9e",
    "spawning_mode": "orchestrator",
    "child_mode": null,
    "timestamp": 1712345678.123
  }
}
```

| Field | Meaning |
|-------|---------|
| `kind` | `"fork"` or `"fresh"` |
| `parent_session_id` | The parent session that spawned this child. May be null on fresh-path if the parent had no session id (rare). |
| `parent_turn_id` | The parent's turn id at spawn time. |
| `spawning_mode` | The parent's mode name when it called the agent tool. |
| `child_mode` | The fresh-path mode name. Null for fork-path (forks inherit). |
| `timestamp` | Unix timestamp at spawn time. |

Spawn metadata is written even if the child errored (so failed runs have inspectable lineage).

### Inspecting via CLI

```bash
aloop sessions info <session_id>
```

prints spawn metadata when present:

```
Session: 7f3a8b1c2d4e5f60
  fork_from:    a7b3c9d2e1f4
  fork_turn_id: 8c2f1d4b6a9e
  fork_depth:   1
  children:     (none)
  messages:     6 stored
  spawn:        fork
  parent:       a7b3c9d2e1f4
  parent_turn:  8c2f1d4b6a9e
  spawning_mode: orchestrator
  child_mode:    (inherited)
  resolved:     14 total
```

### Programmatic access

```python
from aloop.session import AgentSession

session = AgentSession.load(session_id)
if session.spawn_metadata:
    print(session.spawn_metadata["kind"])
    print(session.spawn_metadata["parent_session_id"])
```

## Permission model

aloop's subagent permission model is **structural**, not runtime. There is no runtime check that "the child can't have more privileges than the parent." Safety comes entirely from the `spawnable_modes` allowlist being conservative by configuration.

### How it works

1. A mode opts in to spawning by listing target modes in `spawnable_modes`.
2. Every target mode must declare `subagent_eligible: true`. This is the explicit "I am willing to be spawned" flag.
3. At spawn time, the agent tool checks: does `mode` exist? Is it in this mode's `spawnable_modes`? Is it `subagent_eligible`?
4. If yes to all three, spawn proceeds. If not, the tool returns an error result.

### Structural escalation prevention

A read-only mode literally cannot list a write-capable mode in its `spawnable_modes` if you don't put it there. There is no "child inherits parent's permissions" logic — the child just runs with whatever the target mode says. So you build the safety into the config:

```jsonc
{
  "modes": {
    "explore": {
      "tools": ["read_file", "grep", "find", "ls"],
      "spawnable_modes": ["explore"],   // can only spawn more explorers
      "subagent_eligible": true
    },
    "worker": {
      "tools": ["read_file", "write_file", "edit_file", "bash"],
      "spawnable_modes": ["explore"],   // worker can spawn read-only helpers
      "subagent_eligible": true
    },
    "orchestrator": {
      "tools": ["*"],
      "spawnable_modes": ["explore", "worker", "reviewer"],
      "can_fork": true
    },
    "reviewer": {
      "tools": ["read_file", "grep", "find", "ls"],
      "spawnable_modes": [],            // terminal — can't spawn anything
      "subagent_eligible": true
    }
  }
}
```

`worker` can spawn `explore` but **cannot** spawn `worker` (no self-loop unless you list it). `explore` can spawn more `explore` agents but **cannot** spawn `worker` (different write capability). `reviewer` is a leaf — it can be spawned but can't spawn anything itself.

### Comparison to Claude Code and OpenHarness

This matches the approach used by Claude Code and OpenHarness. From research into both codebases:

- **Claude Code** does not enforce "child can't have more than parent" at runtime. Each subagent type is pre-scoped at definition time. Safety comes from the definitions being conservative, not from runtime permission comparison.
- **OpenHarness** uses the same pattern: agent definitions are static, safety is in the catalog.
- **Neither system** implements per-agent token/cost budgets. Claude Code subagents explicitly bypass budget checks; only `maxTurns` exists. aloop's `max_iterations` per mode plays the same role.

aloop's `spawnable_modes` is the same idea, made explicit in config rather than buried in agent registration code.

## Recursive spawning

aloop allows all four composition patterns:

| Composition | Allowed? |
|-------------|----------|
| Fork → Fork | Yes. The fork child inherits its parent's mode label, so it sees the same `can_fork` and `spawnable_modes`. |
| Fork → Fresh | Yes. Same mode inheritance — the fork child can fresh-spawn any mode in its `spawnable_modes`. |
| Fresh → Fork | Yes, **if** the fresh-path target mode has `can_fork: true`. |
| Fresh → Fresh | Yes, **if** the fresh-path target mode has its own non-empty `spawnable_modes`. |

There is **no artificial depth limit**. Claude Code blocks fork→fork because of prompt cache coherency constraints (their fork mechanism requires byte-identical prefixes). aloop's fork mechanism is session-based (parent pointers, see [Sessions & Forking](SESSIONS.md)) and does not have that constraint.

The depth-10 auto-materialize in `AgentSession.resolve_messages()` is the only safety valve: when a fork chain hits depth 10, the deepest session is materialized to bound chain walks. This is a structural cap on disk-walk cost, not a policy cap on subagent depth.

In practice, real workflows rarely exceed depth 3-4.

## Library API

Subagents are usable from the embedding API in two ways: via the auto-injected agent tool (the model decides), or directly via the executor (you decide).

### AgentExecutor protocol

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class AgentExecutor(Protocol):
    async def spawn(
        self,
        *,
        prompt: str,
        mode: str | None,
        model: str | None,
        parent_session_id: str | None,
        parent_turn_id: str | None,
        fork: bool,
        parent_loop: "ALoop",
    ) -> AgentExecutionHandle: ...
```

`spawn()` returns an `AgentExecutionHandle` — a wrapper around an `asyncio.Task` that resolves to an `AgentResult`. Callers `await handle.result()` to retrieve the result, or `handle.cancel()` to cancel the underlying task.

### InProcessExecutor

The sole built-in implementation. Runs child agents in the same Python process. Fork-path children reuse the parent's `ALoop` instance (so they share its provider/api_key/config and inherit per-stream state setup). Fresh-path children get a new `ALoop` built from the parent's defaults via `_build_fresh_loop`.

```python
from aloop import ALoop, InProcessExecutor, AgentResult

backend = ALoop(
    model="x-ai/grok-4.1-fast",
    api_key="sk-or-...",
    executor=InProcessExecutor(),  # default — explicit here for clarity
)
```

You can swap in a custom executor by passing `executor=` to the `ALoop` constructor. The protocol exists so a future subprocess backend can be added without touching the agent tool.

### Programmatic spawning

The normal pattern is to let the model invoke the agent tool itself. For tests, scripts, or programmatic orchestration you can call the executor directly:

```python
import asyncio
from aloop import ALoop, EventType

async def main():
    backend = ALoop(model="x-ai/grok-4.1-fast")

    # Run a parent stream so we have a session and a turn id
    session_id = "my-orchestrator"
    async for event in backend.stream(
        "Read README.md and tell me the main features.",
        session_id=session_id,
    ):
        if event.type == EventType.LOOP_END:
            parent_text = event.data["text"]
            parent_turn = event.turn_id  # last turn

    # Spawn a fork child directly
    handle = await backend.executor.spawn(
        prompt="Now check src/aloop/__init__.py and list the public exports.",
        mode=None,                          # fork path
        model=None,
        parent_session_id=session_id,
        parent_turn_id=parent_turn,
        fork=True,
        parent_loop=backend,
    )
    fork_result = await handle.result()
    print(fork_result.text)

    # Spawn a fresh child directly (mode must be defined in .aloop/config.json)
    handle = await backend.executor.spawn(
        prompt="Run pytest -q and report the count of passing tests.",
        mode="reviewer",
        model=None,
        parent_session_id=None,
        parent_turn_id=None,
        fork=False,
        parent_loop=backend,
    )
    fresh_result = await handle.result()
    print(fresh_result.text)

asyncio.run(main())
```

## Token accounting

The two paths handle token counters asymmetrically by design.

### Fork path: rolls up

Fork children share the parent's `ALoop` instance. The executor snapshots the parent's `_input_tokens` and `_output_tokens` before the spawn, then **adds** the child's usage delta back into the parent's counters in the restore step. The result: the child's token usage shows up in `parent.cost_usd` and the parent's `LOOP_END` event totals.

This matches the cost model: forks share the prompt cache, the parent pays the same provider bill for both, and rolling up keeps `parent.cost_usd` accurate.

### Fresh path: independent

Fresh children get their own `ALoop` instance with isolated counters. Their tokens never roll up into the parent. The parent sees the child's usage on the per-call result via `AgentResult.usage` (and on the agent tool's `details` dict), but it does not affect the parent's running totals.

This matches the cost model: fresh children may run on a different model and have their own session-level cost tracking. Aggregating them into the parent's counters would conflate two different model costs.

If you want a unified rollup across both paths, sum the `AgentResult.usage` values yourself (or instrument via hooks).

## Error handling

The agent tool distinguishes two error categories:

### Validation errors → ToolResult(is_error=True)

These return a `ToolResult` with `is_error=True` and a human-readable message. The model sees the error and can adapt:

- Fork-path called from a mode where `can_fork` is false
- Fork-path called without a persistent parent session
- Mode name not in `spawnable_modes` for the current mode
- Mode name not defined in project config
- Target mode not `subagent_eligible`

Example:

```
agent: mode 'writer' is not in the allowed spawnable_modes for the current mode.
Allowed: explore, reviewer
```

### Infrastructure errors → propagate

If the child loop emits an `ERROR` event, the executor raises `InferenceError(error_message)` from `handle.result()`. The agent tool's `_spawn_agent` catches this and returns a `ToolResult(is_error=True, content=f"agent: spawn failed: {exc}")`. Spawn metadata is still written to the child session before the error is raised, so failed runs are inspectable via `aloop sessions info`.

## Limitations

These are deliberate scope limits in v0.6.0, not bugs:

- **No per-agent resource budgets.** No token cap, no cost cap, no wall-clock cap. `max_iterations` per mode is the only ceiling. This matches Claude Code and OpenHarness — neither implements per-agent budgets either. Add via hooks if you need them.
- **No subprocess executor.** `AgentExecutor` is a protocol so a subprocess backend can be added, but the only implementation is `InProcessExecutor`. The case for subprocess (crash isolation, OS-level resource limits) is real but unproven for an in-process library.
- **No inter-agent messaging.** Subagent execution is tree-shaped: parent calls child, child returns result, parent resumes. There is no sibling-to-sibling channel and no parent-to-running-child channel.
- **No parallel sibling execution within a turn.** Multiple agent tool calls in a single turn are run **sequentially** in v0.6.0. The executor returns a Task, but the agent tool awaits it before returning. Concurrent fan-out is a future enhancement.
- **No event bubbling.** The parent does not see streaming events from the child's loop. Consumers can subscribe to the child session directly via aloop's existing event streaming if they want real-time visibility.
- **No artifact handoff mechanism.** Children write files; parents read them. This is a prompt convention (the agent tool description tells children to write large output to files), not library infrastructure.

The rationale for each of these is that neither Claude Code nor OpenHarness ships them either. The protocol boundary (`AgentExecutor`) exists so they can be added later without rewriting the agent tool.
