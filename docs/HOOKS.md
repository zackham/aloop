# Hook System

aloop's hook system lets projects extend the agent loop without modifying harness code. Hooks are Python files in `.aloop/hooks/` that use the `@hook` decorator.

## Extension Points

| Point | When It Runs | Signature | Returns |
|-------|-------------|-----------|---------|
| `before_tool` | Before each tool execution | `(name, args, **ctx)` | `{"allow": bool, "reason": str, "modified_args": dict}` |
| `after_tool` | After each tool execution | `(name, args, result, **ctx)` | `{"modified_result": str}` |
| `gather_context` | When building the system prompt | `(**kwargs)` | `str` |
| `register_tools` | On initialization | `()` | `list[ToolDef]` |

All hooks are optional. The harness works without any hooks configured.

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

For `before_tool`, the first hook that returns `{"allow": False}` stops execution — later hooks don't run.

## Context Parameter

`before_tool` and `after_tool` hooks receive a `**ctx` dict with:

| Key | Description |
|-----|-------------|
| `session_key` | Session ID (if persistent session) |
| `session_key` | Session ID (if persistent session) |
| `topic_id` | External topic ID (if provided) |
| `chat_id` | External chat ID (if provided) |
| `_capability_token` | Permission token (if provided by caller) |

## Extension Point Details

### before_tool

Runs before every tool call. Can block execution or modify arguments.

```python
@hook("before_tool", priority=10)
def require_approval(name: str, args: dict, **ctx) -> dict:
    # Block destructive tools in certain contexts
    dangerous = {"write_file", "edit_file"}
    if name in dangerous and "production" in args.get("path", ""):
        return {"allow": False, "reason": f"Tool '{name}' blocked for production paths"}
    return {"allow": True}
```

Return values:
- `{"allow": True}` — proceed (default if no hooks)
- `{"allow": False, "reason": "..."}` — block, error message shown to model
- `{"allow": True, "modified_args": {...}}` — proceed with modified arguments

### after_tool

Runs after tool execution. Can modify the result before it's shown to the model.

```python
@hook("after_tool")
def redact_secrets(name: str, args: dict, result: str, **ctx) -> dict:
    # Strip API keys from tool output
    import re
    cleaned = re.sub(r'sk-[a-zA-Z0-9]{32,}', '[REDACTED]', result)
    return {"modified_result": cleaned}
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

```python
from aloop import ToolDef, ToolResult

@hook("register_tools")
def project_tools() -> list:
    async def _search(query: str) -> ToolResult:
        # Project-specific search implementation
        return ToolResult(content=f"Results for: {query}")

    search_tool = ToolDef(
        name="search",
        description="Search the project's knowledge base",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        execute=_search,
    )
    return [search_tool]
```

## Discovery

Hooks are discovered lazily on first use:
1. Scan `.aloop/hooks/` for `*.py` files (skipping `_`-prefixed files)
2. Import each file via `importlib`
3. Register any functions with `@hook` decorators
4. Sort by priority per extension point
5. Cache — discovery runs once per process

If a hook file fails to import, a warning is logged and the file is skipped. Hook failures never crash the harness.

## Testing Hooks

```python
from aloop.hooks import reset

def test_my_hook():
    reset()  # clear hook cache
    # ... test setup ...
```

The `reset()` function clears all discovered hooks and resets the discovery flag, allowing re-discovery in tests.
