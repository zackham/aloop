# Embedding aloop

This guide covers using aloop as a Python library inside your own applications — the primary use case.

## Minimal example

```python
import asyncio
from aloop import ALoop, EventType

async def main():
    backend = ALoop(
        model="x-ai/grok-4.1-fast",
        api_key="sk-or-...",
    )

    async for event in backend.stream("What is 2+2?"):
        if event.type == EventType.TEXT_DELTA:
            print(event.data["text"], end="")
        elif event.type == EventType.LOOP_END:
            print(f"\nCost: ${event.data['cost_usd']:.4f}")

asyncio.run(main())
```

That's a working agent with tool access in 12 lines.

## ALoop

The main class. Each instance is a stateful agent with its own token counters, session, and compaction state.

### Constructor

```python
from aloop import ALoop, LoopConfig
from aloop.compaction import CompactionSettings

ALoop(
    model="x-ai/grok-4.1-fast",      # any model ID (required)
    api_key="sk-or-...",               # or set via env var
    provider="openrouter",             # provider name or ProviderConfig
    config=LoopConfig(
        max_iterations=50,
        max_session_age=14400.0,
        max_session_messages=100,
        compaction=CompactionSettings(
            reserve_tokens=16_384,
            keep_recent_tokens=20_000,
            compact_instructions="Preserve error messages and stack traces.",
        ),
    ),
)
```

**Provider resolution order:** explicit `provider` arg → `~/.aloop/config.json` `"provider"` key → `"openrouter"`.

**API key resolution order:** explicit `api_key` arg → provider-specific env var (e.g. `OPENROUTER_API_KEY`) → `ALOOP_API_KEY` env var → `~/.aloop/credentials.json`.

### stream()

The primary interface. Returns an async iterator of events.

```python
async for event in backend.stream(
    "your prompt",
    system_prompt="Custom system prompt",    # optional, overrides default
    tools=[your_tool_defs],                  # optional, REPLACES entire tool set
    extra_tools=[additional_tools],          # optional, extends default tool set
    session_id="my-session",                 # optional, enables persistence
    mode="review",                           # optional, named mode config
    inject_context=True,                     # optional, runs gather_context hooks (default True)
    persist_session=True,                    # optional, set False to disable persistence
    response_format={"type": "json_object"}, # optional, for structured output
    context={"user_id": "abc123"},           # optional, forwarded to hooks
    fork_from="parent_session_id",           # optional, fork from another session
    fork_at="turn_005",                      # optional, turn_id to fork at (default: last turn)
    replace_turn="turn_003",                 # optional, edit+rerun — truncate and replace a turn
):
    ...
```

### Using modes

Named modes from `.aloop/config.json` configure model, tools, system prompt, compaction, and iteration limits per session:

```python
# Use a review mode — read-only tools, reviewer system prompt
async for event in backend.stream(
    "Review auth.py for security issues",
    mode="review",
    session_id="review-session",
):
    ...

# Use a fast mode — different model, fewer iterations
result = await backend.run("Quick question", mode="fast")

# Explicit kwargs override mode settings
async for event in backend.stream(
    "Check this file",
    mode="review",
    system_prompt="Custom override.",  # overrides mode's system_prompt
):
    ...
```

**Mode conflict detection:** once a session is created with a mode, calling `stream()` with a *different* mode on the same `session_id` raises `ModeConflictError`:

```python
from aloop import ModeConflictError

# First call creates session with "review" mode
await backend.run("Review code", session_id="s1", mode="review")

# Second call with different mode on same session — raises
try:
    await backend.run("Write code", session_id="s1", mode="code")
except ModeConflictError as e:
    print(e)  # "Session 's1' was created with mode 'review', cannot switch to 'code'."
```

See [Config](CONFIG.md) for full mode configuration reference and [Permissions](PERMISSIONS.md) for tool sets and path restrictions.

### Tool sets

aloop ships three tool sets. Use `tools=` on `stream()` or configure via modes.

```python
from aloop import CODING_TOOLS, READONLY_TOOLS, ALL_TOOLS

# Default — full access (read, write, edit, bash, skill)
async for event in backend.stream("Fix the bug", tools=CODING_TOOLS):
    ...

# Safe exploration — no shell, no writes (read, grep, find, ls, skill)
async for event in backend.stream("What does this codebase do?", tools=READONLY_TOOLS):
    ...

# Everything
async for event in backend.stream("Explore and fix", tools=ALL_TOOLS):
    ...
```

### Forking sessions

Fork a session to branch at any turn — for subagent patterns, edit+rerun, or exploring alternatives.

```python
# Fork from a specific turn
async for event in backend.stream(
    "try a different approach",
    fork_from="parent_session_id",
    fork_at="turn_005",
):
    ...

# Fork from the latest turn (fork_at defaults to last)
async for event in backend.stream(
    "explore this alternative",
    fork_from="parent_session_id",
):
    ...

# Edit+rerun: truncate at a turn and replace with new prompt
async for event in backend.stream(
    "better prompt",
    session_id="existing_session",
    replace_turn="turn_003",
):
    ...
```

See [Sessions & Forking](SESSIONS.md) for the full model — materialization, garbage collection, compaction interaction, and design rationale.

### PermissionDenied

When a tool call is blocked by permissions, the model receives a `PermissionDenied` error (subclass of `ToolRejected`). The model can then adapt its approach.

```python
from aloop import PermissionDenied
```

### run()

Convenience wrapper. Consumes the stream and returns the final result.

```python
result = await backend.run("What is 2+2?")
print(result.text)           # "4"
print(result.cost_usd)       # 0.001
print(result.input_tokens)   # 150
print(result.output_tokens)  # 20
print(result.model)          # "x-ai/grok-4.1-fast"
print(result.turns)          # 1
```

Returns a `RunResult` dataclass. Raises `InferenceError` on failure.

## Event types

Every event from `stream()` is an `InferenceEvent(type, data, timestamp, session_id, turn_id, tool_call_id)`:

| Event | Data | When |
|-------|------|------|
| `LOOP_START` | `{"session_id": "...", "model": "...", "provider": "..."}` | Loop begins (before first turn) |
| `TURN_START` | `{"iteration": 0, "turn_id": "..."}` | Agent loop iteration begins |
| `TEXT_DELTA` | `{"text": "..."}` | Model produces text (streaming) |
| `THINKING_DELTA` | `{"text": "..."}` | Model thinking/reasoning output |
| `TOOL_START` | `{"name": "bash", "id": "call_1", "args": {"command": "ls"}}` | Model requests a tool call |
| `TOOL_DELTA` | `{"name": "bash", "id": "call_1", "output": "..."}` | Streaming tool output (bash) |
| `TOOL_END` | `{"name": "bash", "id": "call_1", "result": "...", "is_error": false}` | Tool execution complete |
| `TURN_END` | `{"iteration": 0, "turn_id": "...", "input_tokens": N, "output_tokens": N, "cost_usd": 0.01}` | Agent loop iteration ends |
| `COMPACTION` | `{"messages_before": N, "messages_after": N, "tokens_saved": N}` | Context compacted |
| `LOOP_END` | `{"text": "...", "session_id": "...", "input_tokens": N, "output_tokens": N, "cost_usd": 0.01, "model": "...", "turns": N}` | Final response |
| `ERROR` | `{"message": "..."}` | Fatal error |

### Consuming events

```python
accumulated_text = ""
tool_calls = []

async for event in backend.stream("Analyze the logs"):
    match event.type:
        case EventType.TEXT_DELTA:
            accumulated_text += event.data["text"]

        case EventType.TOOL_START:
            tool_calls.append(event.data["name"])

        case EventType.TOOL_END:
            if event.data["is_error"]:
                print(f"Tool {event.data['name']} failed: {event.data['result']}")

        case EventType.LOOP_END:
            print(f"Done. {event.data['input_tokens']} in, {event.data['output_tokens']} out")
```

## Custom tools

The `@tool` decorator is the easiest way to define tools. It builds a `ToolDef` from type hints:

```python
from typing import Annotated
from aloop import tool, ToolParam, ToolResult

@tool(name="query_db", description="Run a SQL query")
async def query_db(
    sql: Annotated[str, ToolParam(description="The SQL query to run")],
    limit: Annotated[int, ToolParam(description="Max rows to return")] = 100,
) -> ToolResult:
    rows = await my_db.execute(sql, limit=limit)
    return ToolResult(content=str(rows))

# query_db IS a ToolDef — pass it directly to stream()
async for event in backend.stream("Show me all users", extra_tools=[query_db]):
    ...
```

The decorator inspects type hints to build JSON Schema automatically. `Annotated` with `ToolParam` adds descriptions. Parameters with defaults are optional; those without are required. Sync functions are auto-wrapped to async.

You can also set a per-tool timeout:

```python
@tool(timeout=30.0)
async def slow_query(sql: str) -> ToolResult:
    ...
```

Register tools via hooks (no source modifications):

```python
# .aloop/hooks/tools.py
from typing import Annotated
from aloop_hooks import hook
from aloop import tool, ToolParam, ToolResult

@hook("register_tools")
def my_tools():
    @tool(name="query_db", description="Run a SQL query")
    async def query_db(
        sql: Annotated[str, ToolParam(description="SQL query")],
    ) -> ToolResult:
        rows = await my_db.execute(sql)
        return ToolResult(content=str(rows))

    return [query_db]
```

Or build `ToolDef` objects manually for full control:

```python
from aloop import ToolDef, ToolResult

my_tool = ToolDef(
    name="get_price",
    description="Get the current price of a stock",
    parameters={
        "type": "object",
        "properties": {"symbol": {"type": "string"}},
        "required": ["symbol"],
    },
    execute=get_price_fn,
)

# Extend default tools (keeps built-ins + hook tools)
async for event in backend.stream(
    "What's AAPL trading at?",
    extra_tools=[my_tool],
):
    ...

# Replace entire tool set (only my_tool is available)
async for event in backend.stream(
    "What's AAPL trading at?",
    tools=[my_tool],
):
    ...
```

### Tool merge behavior

Tools are assembled in this order:

1. Mode defines base tools (or defaults if no mode)
2. `register_tools` hooks add to the set
3. `extra_tools=` extends the current set
4. `tools=` **replaces** the entire set (skips 1-3)

## Passing context to hooks

The `context` dict on `stream()` is forwarded to hook functions as `**ctx`:

```python
# Your application code
async for event in backend.stream(
    "Check the dashboard",
    context={
        "user_id": "user-42",
        "environment": "staging",
        "permissions": ["read", "write"],
    },
):
    ...
```

```python
# .aloop/hooks/permissions.py
from aloop_hooks import hook

@hook("before_tool")
def check_permissions(name: str, args: dict, **ctx) -> dict:
    perms = ctx.get("permissions", [])
    if name == "write_file" and "write" not in perms:
        return {"allow": False, "reason": "No write permission"}
    return {"allow": True}
```

This is how embedding applications provide domain-specific context (user identity, permission levels, feature flags, etc.) to the hook system without modifying aloop.

### Hook lifecycle

aloop provides 10 hook points for full lifecycle control. See [HOOKS.md](HOOKS.md) for complete documentation.

**Lifecycle hooks** (called with `context` dict):
- `on_loop_start` / `on_loop_end` — around the entire stream
- `on_turn_start` / `on_turn_end` — around each agent turn
- `on_pre_compaction` / `on_post_compaction` — around context compaction

**Tool hooks** (called with tool name, args, and `**ctx`):
- `before_tool` — can block, modify args, or short-circuit with cached result
- `after_tool` — can modify tool output

**Other hooks**:
- `gather_context` — inject context into the system prompt
- `register_tools` — register additional tools

### ToolRejected

`before_tool` hooks can raise `ToolRejected` to cancel a tool call:

```python
from aloop import ToolRejected

@hook("before_tool")
def enforce_policy(name, args, **ctx):
    if ctx.get("environment") == "production" and name == "write_file":
        raise ToolRejected("Write operations blocked in production")
```

Hooks can also return a `ToolResult` to short-circuit execution with a cached/mocked response without calling the actual tool.

## Sessions

Sessions persist conversation history to `~/.aloop/sessions/` and survive across `stream()` calls.

```python
# First call — creates session
async for event in backend.stream("Read README.md", session_id="my-task"):
    ...

# Second call — same backend, same session_id — has context from first call
async for event in backend.stream("Now summarize it", session_id="my-task"):
    ...
```

Sessions auto-clear when stale (default: 4 hours or 100 messages). Configure via `LoopConfig`:

```python
from aloop import ALoop, LoopConfig

backend = ALoop(
    model="x-ai/grok-4.1-fast",
    config=LoopConfig(
        max_session_age=3600.0,       # 1 hour
        max_session_messages=50,
    ),
)
```

Disable persistence for one-shot calls:

```python
result = await backend.run("What is 2+2?", persist_session=False)
```

## System prompt control

By default, aloop uses its built-in system prompt (see [SYSTEM-PROMPT.md](SYSTEM-PROMPT.md)). Override per-call:

```python
async for event in backend.stream(
    "Analyze this data",
    system_prompt="You are a data analyst. Be precise. Use tables.",
):
    ...
```

Or configure project-wide via `.aloop/config.json` and `AGENTS.md`. See [SYSTEM-PROMPT.md](SYSTEM-PROMPT.md).

## Structured output

Request JSON output from the model:

```python
result = await backend.run(
    "List the top 3 files by size as JSON",
    response_format={"type": "json_object"},
)
data = json.loads(result.text)
```

## Providers

Switch providers programmatically:

```python
from aloop.providers import ProviderConfig

# Use a built-in provider
backend = ALoop(model="gpt-4o", provider="openai")

# Use a custom provider
my_provider = ProviderConfig(
    name="Internal LLM",
    base_url="https://llm.corp.internal/v1/chat/completions",
    env_key="CORP_LLM_KEY",
)
backend = ALoop(model="our-model", provider=my_provider)
```

## Error handling

```python
from aloop import InferenceError

try:
    result = await backend.run("Do something risky")
except InferenceError as e:
    print(f"Agent failed: {e}")
```

With streaming, errors arrive as events:

```python
async for event in backend.stream("Do something"):
    if event.type == EventType.ERROR:
        print(f"Error: {event.data['message']}")
        break
```

## Complete example: a CLI tool that uses aloop

```python
#!/usr/bin/env python3
"""Simple CLI that uses aloop to answer questions about a codebase."""

import asyncio
import sys
from aloop import ALoop, EventType, ToolDef, ToolResult

async def main():
    question = " ".join(sys.argv[1:]) or "What does this codebase do?"

    backend = ALoop(
        model="x-ai/grok-4.1-fast",
        max_iterations=10,
    )

    async for event in backend.stream(question):
        if event.type == EventType.TEXT_DELTA:
            sys.stdout.write(event.data["text"])
        elif event.type == EventType.LOOP_END:
            sys.stderr.write(f"\n[{event.data.get('model', '?')} | ${event.data.get('cost_usd', 0):.4f}]\n")

asyncio.run(main())
```

Save as `ask.py`, run: `python ask.py "How does the auth system work?"`
