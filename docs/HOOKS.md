# Hook System

aloop's hook system lets projects extend the agent loop without modifying harness code. Hooks are Python files that use the `@hook` decorator.

## Hook Directories

Hooks load from two locations:

1. **Global**: `~/.aloop/hooks/` — user-wide hooks (run first)
2. **Project**: `.aloop/hooks/` — project-specific hooks (run second)

Both directories are scanned. Global hooks run before project hooks. If a hook file with the same name exists in both directories, the project version replaces the global version.

To disable specific global hooks in a project, use `disabled_hooks` in `.aloop/config.json`:

```json
{
  "disabled_hooks": ["audit", "logging"]
}
```

## Extension Points (10 hooks)

| Point | When It Runs | Signature | Returns |
|-------|-------------|-----------|---------|
| `on_loop_start` | Start of `stream()` | `(context: dict)` | `None` |
| `on_loop_end` | End of `stream()` | `(context: dict, result: dict)` | `None` |
| `on_turn_start` | Start of each agent turn | `(context: dict)` | `None` |
| `on_turn_end` | End of each agent turn | `(context: dict, result: dict)` | `None` |
| `before_tool` | Before each tool execution | `(name, args, **ctx)` | See below |
| `after_tool` | After each tool execution | `(name, args, result, **ctx)` | `{"modified_result": str}` |
| `on_pre_compaction` | Before context compaction | `(context: dict)` | `str \| None` (extra instructions) |
| `on_post_compaction` | After context compaction | `(context: dict)` | `None` |
| `gather_context` | When building the system prompt | `(**kwargs)` | `str` |
| `register_tools` | On initialization | `()` | `list[ToolDef]` |

All hooks are optional. The harness works without any hooks configured. Hook failures are logged but never crash the harness.

## Execution Order

1. Global hooks run first, project hooks second
2. Alphabetical within each scope
3. `before_tool`: first rejection short-circuits — later hooks don't run
4. `gather_context`: all results concatenated in order
5. `on_pre_compaction`: all returned strings concatenated as extra compaction instructions

## Writing a Hook

Create a `.py` file in `.aloop/hooks/` (not starting with `_`):

```python
# .aloop/hooks/safety.py
from aloop_hooks import hook

@hook("before_tool", priority=10)
def block_rm_rf(name: str, args: dict, **ctx) -> dict:
    if name == "bash" and "rm -rf" in args.get("command", ""):
        return {"allow": False, "reason": "Blocked: destructive command"}
    return {"allow": True}
```

The `aloop_hooks` module is automatically available to hook files — no installation needed.

## Priority

Lower numbers run first. Default is 50.

```python
@hook("before_tool", priority=5)   # runs first
def permissions(name, args, **ctx): ...

@hook("before_tool", priority=10)  # runs second
def firebreaks(name, args, **ctx): ...

@hook("before_tool")               # priority=50, runs last
def logging(name, args, **ctx): ...
```

## Context Parameter

Lifecycle hooks (`on_loop_start`, `on_loop_end`, etc.) receive a `context` dict with:

| Key | Description |
|-----|-------------|
| `session_id` | Session ID (if persistent session) |
| `model` | Model ID (on loop hooks) |
| `provider` | Provider name (on loop hooks) |
| `iteration` | Turn iteration number (on turn hooks) |
| `turn_id` | Turn ID (on turn hooks) |

`before_tool` and `after_tool` hooks receive `**ctx` with `session_id` and any additional keys passed via the `context` dict on `stream()`.

## Extension Point Details

### on_loop_start

Called at the very start of `stream()`, after the LOOP_START event is emitted.

```python
@hook("on_loop_start")
def track_session(context: dict):
    print(f"Session {context['session_id']} started with {context['model']}")
```

### on_loop_end

Called just before the LOOP_END event is emitted.

```python
@hook("on_loop_end")
def log_completion(context: dict, result: dict):
    print(f"Session completed: {result['turns']} turns, ${result.get('cost_usd', 0):.4f}")
```

### on_turn_start / on_turn_end

Called at the start and end of each agent turn (one LLM call + tool execution cycle).

```python
@hook("on_turn_start")
def track_turn(context: dict):
    print(f"Turn {context['iteration']} starting")

@hook("on_turn_end")
def log_turn(context: dict, result: dict):
    print(f"Turn {context['iteration']}: {result['input_tokens']} in, {result['output_tokens']} out")
```

### before_tool

Runs before every tool call. Can block execution, modify arguments, or short-circuit with a cached result.

**Return contract:**

- Return `None` → proceed (same as `allow=True`)
- Raise `ToolRejected(reason)` → cancel, reason passed to model as error
- Return `ToolResult(content=...)` → short-circuit with this result (mock/cache)
- Return `{"allow": False, "reason": "..."}` → cancel (backward compat)
- Return `{"allow": True, "modified_args": {...}}` → proceed with modified arguments

```python
from aloop import ToolRejected, ToolResult

@hook("before_tool", priority=10)
def require_approval(name: str, args: dict, **ctx) -> dict:
    # Block destructive tools
    if name == "bash" and "rm -rf" in args.get("command", ""):
        raise ToolRejected("Destructive command blocked")
    return None  # proceed

@hook("before_tool", priority=20)
def cache_reads(name: str, args: dict, **ctx):
    # Return cached result for read_file
    if name == "read_file" and args.get("path") in my_cache:
        return ToolResult(content=my_cache[args["path"]])
    return None
```

### after_tool

Runs after tool execution. Can modify the result before it's shown to the model.

```python
@hook("after_tool")
def redact_secrets(name: str, args: dict, result: str, **ctx) -> dict:
    import re
    cleaned = re.sub(r'sk-[a-zA-Z0-9]{32,}', '[REDACTED]', result)
    return {"modified_result": cleaned}
```

### on_pre_compaction

Runs before context compaction. Can return extra instructions for the compaction summarizer.

```python
@hook("on_pre_compaction")
def preserve_api_details(context: dict) -> str:
    return "Preserve all API endpoint URLs and response schemas in the summary."
```

Multiple hooks' return values are concatenated and appended to the compaction instructions.

### on_post_compaction

Runs after context compaction completes.

```python
@hook("on_post_compaction")
def log_compaction(context: dict):
    saved = context.get("tokens_saved", 0)
    print(f"Compaction saved {saved} tokens")
```

### gather_context

Runs when building the system prompt. Returns a string to append as context.

```python
@hook("gather_context")
def inject_daily_notes(**kwargs) -> str:
    from pathlib import Path
    notes = Path("notes.md")
    if notes.exists():
        return f"## Today's Notes\n\n{notes.read_text()[:2000]}"
    return ""
```

Multiple `gather_context` hooks are concatenated with `\n\n`.

### register_tools

Runs on initialization. Returns additional ToolDef objects to add to the tool set.

Using the `@tool` decorator (recommended):

```python
from typing import Annotated
from aloop import tool, ToolParam, ToolResult

@hook("register_tools")
def project_tools() -> list:
    @tool(description="Search the project's knowledge base")
    async def search(
        query: Annotated[str, ToolParam(description="Search query")],
    ) -> ToolResult:
        return ToolResult(content=f"Results for: {query}")

    return [search]
```

Or with manual ToolDef construction:

```python
from aloop import ToolDef, ToolResult

@hook("register_tools")
def project_tools() -> list:
    async def _search(query: str) -> ToolResult:
        return ToolResult(content=f"Results for: {query}")

    return [ToolDef(
        name="search",
        description="Search the project's knowledge base",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        execute=_search,
    )]
```

## Tool Merge Behavior

Tools are assembled in this order:

1. **Mode base** — if `mode=` is set on `stream()`, the mode's tool list is the starting point
2. **Hook tools** — `register_tools` hooks add to the set
3. **`extra_tools=`** — extends the current set (additive)
4. **`tools=`** — **replaces the entire set** (explicit override, skips steps 1-3)

```python
# Uses defaults + hook tools + extra_tools
async for event in backend.stream("prompt", extra_tools=[my_tool]):
    ...

# Replaces everything — only custom_tool is available
async for event in backend.stream("prompt", tools=[custom_tool]):
    ...
```

## ToolRejected Exception

`ToolRejected` is a purpose-built exception for `before_tool` hooks to cancel tool execution:

```python
from aloop import ToolRejected

@hook("before_tool")
def enforce_policy(name: str, args: dict, **ctx):
    if name == "bash" and "--force" in args.get("command", ""):
        raise ToolRejected("Force flags are not permitted")
```

When raised, the tool call is skipped and the reason string is returned to the model as an error result. This is more explicit than the dict-based `{"allow": False}` pattern and integrates with Python's exception handling.

## Discovery

Hooks are discovered lazily on first use:
1. Scan `~/.aloop/hooks/` (global) for `*.py` files (skipping `_`-prefixed)
2. Scan `.aloop/hooks/` (project) for `*.py` files (skipping `_`-prefixed)
3. Import each file via `importlib`
4. Register any functions with `@hook` decorators
5. Filter out hooks listed in `disabled_hooks` config
6. Merge: same-name hook file in project replaces global version
7. Sort by priority per extension point
8. Cache — discovery runs once per process

If a hook file fails to import, a warning is logged and the file is skipped. Hook failures never crash the harness.

## Testing Hooks

```python
from aloop.hooks import reset

def test_my_hook():
    reset()  # clear hook cache
    # ... test setup ...
```

The `reset()` function clears all discovered hooks and resets the discovery flag, allowing re-discovery in tests.
