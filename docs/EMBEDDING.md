# Embedding aloop

This guide covers using aloop as a Python library inside your own applications — the primary use case.

## Minimal example

```python
import asyncio
from aloop import AgentLoopBackend, EventType

async def main():
    backend = AgentLoopBackend(
        model="x-ai/grok-4.1-fast",
        api_key="sk-or-...",
    )

    async for event in backend.stream("What is 2+2?"):
        if event.type == EventType.TEXT_DELTA:
            print(event.data["text"], end="")
        elif event.type == EventType.COMPLETE:
            print(f"\nCost: ${event.data['usage']['cost_usd']:.4f}")

asyncio.run(main())
```

That's a working agent with tool access in 12 lines.

## AgentLoopBackend

The main class. Each instance is a stateful agent with its own token counters, session, and compaction state.

### Constructor

```python
AgentLoopBackend(
    model="x-ai/grok-4.1-fast",      # any model ID (required)
    api_key="sk-or-...",               # or set via env var
    provider="openrouter",             # provider name or ProviderConfig
    max_iterations=50,                 # max tool-call loop iterations
    compaction_settings=CompactionSettings(
        reserve_tokens=16_384,
        keep_recent_tokens=20_000,
        compact_instructions="Preserve error messages and stack traces.",
    ),
    max_session_age=14400.0,           # stale session threshold (seconds)
    max_session_messages=100,          # stale session threshold (messages)
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
    tools=[your_tool_defs],                  # optional, overrides built-in tools
    session_key="my-session",                # optional, enables persistence
    inject_context=True,                     # optional, runs gather_context hooks
    persist_session=True,                    # optional, set False to disable persistence
    response_format={"type": "json_object"}, # optional, for structured output
    # any extra kwargs are passed through to hooks as **ctx
    user_id="abc123",
    environment="production",
):
    ...
```

### run()

Convenience wrapper. Consumes the stream and returns the final result.

```python
result = await backend.run("What is 2+2?")
print(result.text)       # "4"
print(result.cost_usd)   # 0.001
print(result.usage)      # {"input_tokens": 150, "output_tokens": 20, ...}
```

Raises `InferenceError` on failure.

## Event types

Every event from `stream()` is an `InferenceEvent(type, data)`:

| Event | Data | When |
|-------|------|------|
| `TEXT_DELTA` | `{"text": "..."}` | Model produces text (streaming) |
| `TOOL_START` | `{"name": "bash", "id": "call_1", "args": {"command": "ls"}}` | Model requests a tool call |
| `TOOL_END` | `{"name": "bash", "id": "call_1", "result": "...", "is_error": false}` | Tool execution complete |
| `TURN_START` | `{"iteration": 0}` | Agent loop iteration begins |
| `COMPLETE` | `{"text": "...", "session_id": "...", "cost_usd": 0.01, "usage": {...}}` | Final response |
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

        case EventType.COMPLETE:
            usage = event.data["usage"]
            print(f"Done. {usage['input_tokens']} in, {usage['output_tokens']} out")
```

## Custom tools

Register tools via hooks (no source modifications):

```python
# .aloop/hooks/tools.py
from aloop_hooks import hook
from aloop import ToolDef, ToolResult

@hook("register_tools")
def my_tools():
    async def _query(sql: str) -> ToolResult:
        rows = await my_db.execute(sql)
        return ToolResult(content=str(rows))

    return [ToolDef(
        name="query_db",
        description="Run a SQL query",
        parameters={
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
        execute=_query,
    )]
```

Or pass tools directly to `stream()`:

```python
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

async for event in backend.stream(
    "What's AAPL trading at?",
    tools=[my_tool],  # replaces built-in tools for this call
):
    ...
```

## Passing context to hooks

Any extra kwargs to `stream()` are forwarded to hook functions as `**ctx`:

```python
# Your application code
async for event in backend.stream(
    "Check the dashboard",
    user_id="user-42",
    environment="staging",
    permissions=["read", "write"],
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

## Sessions

Sessions persist conversation history to `~/.aloop/sessions/` and survive across `stream()` calls.

```python
# First call — creates session
async for event in backend.stream("Read README.md", session_key="my-task"):
    ...

# Second call — same backend, same session_key — has context from first call
async for event in backend.stream("Now summarize it", session_key="my-task"):
    ...
```

Sessions auto-clear when stale (default: 4 hours or 100 messages). Configure via constructor:

```python
backend = AgentLoopBackend(
    model="x-ai/grok-4.1-fast",
    max_session_age=3600.0,       # 1 hour
    max_session_messages=50,
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
backend = AgentLoopBackend(model="gpt-4o", provider="openai")

# Use a custom provider
my_provider = ProviderConfig(
    name="Internal LLM",
    base_url="https://llm.corp.internal/v1/chat/completions",
    env_key="CORP_LLM_KEY",
)
backend = AgentLoopBackend(model="our-model", provider=my_provider)
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
from aloop import AgentLoopBackend, EventType, ToolDef, ToolResult

async def main():
    question = " ".join(sys.argv[1:]) or "What does this codebase do?"

    backend = AgentLoopBackend(
        model="x-ai/grok-4.1-fast",
        max_iterations=10,
    )

    async for event in backend.stream(question):
        if event.type == EventType.TEXT_DELTA:
            sys.stdout.write(event.data["text"])
        elif event.type == EventType.COMPLETE:
            usage = event.data.get("usage", {})
            sys.stderr.write(f"\n[{usage.get('model', '?')} | ${usage.get('cost_usd', 0):.4f}]\n")

asyncio.run(main())
```

Save as `ask.py`, run: `python ask.py "How does the auth system work?"`
