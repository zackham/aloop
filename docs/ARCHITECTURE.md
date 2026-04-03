# Architecture

## Overview

aloop is an agent loop harness — it sends prompts to language models, executes tool calls, and loops until the model produces a final text response.

```
┌─────────────────────────────────────────────────────────────┐
│                        aloop                                │
│                                                             │
│   System Prompt ──► Agent Loop ──► OpenRouter API           │
│   (.aloop/config)       │          (any model)              │
│                         │                                   │
│                    ┌────▼────┐                              │
│                    │  Tools  │                              │
│                    │ ┌─────┐ │  ┌──────────┐               │
│                    │ │files│ │  │  Hooks   │               │
│                    │ │bash │ │  │(.aloop/) │               │
│                    │ │skill│ │  └──────────┘               │
│                    │ └─────┘ │                              │
│                    └─────────┘                              │
│                         │                                   │
│                    ┌────▼────┐                              │
│                    │Sessions │                              │
│                    │(persist)│                              │
│                    └─────────┘                              │
└─────────────────────────────────────────────────────────────┘
```

## Data Flow

### Per API Call

```
1. Build system prompt
   ├── Template mode: read ALOOP-PROMPT.md, interpolate {{variables}}
   └── Section mode: assemble defaults + overrides + AGENTS.md

2. Assemble messages
   ├── System prompt (position 0, cached by prefix matching)
   ├── Session history (if resuming)
   └── New user prompt

3. Run hooks
   ├── gather_context → append to system prompt
   └── register_tools → extend tool set

4. Send to OpenRouter
   ├── Model selected by --model flag
   ├── Tools passed as separate API parameter (JSON schemas)
   └── Stream response (SSE)

5. Process response
   ├── Text deltas → stream to terminal
   ├── Tool calls → execute each:
   │   ├── run before_tool hooks (allow/block/modify)
   │   ├── execute tool function
   │   ├── run after_tool hooks (transform result)
   │   └── persist large results to disk (>50K chars)
   └── No tool calls → COMPLETE (exit loop)

6. Post-turn
   ├── Check compaction threshold
   │   ├── Summarize old context if over limit
   │   ├── Re-read recently accessed files
   │   └── Circuit breaker (3 consecutive failures → stop trying)
   └── Save session to disk
```

### Session Lifecycle

```
Fresh session:
  messages = [] → add user prompt → send → get response → save

Resumed session:
  messages = [persisted history] → add new prompt → send → save

Stale session (>4h or >100 messages):
  auto-clear → treated as fresh
```

## Streaming Protocol

All events are `InferenceEvent(type, data)`:

| Event | Data | When |
|-------|------|------|
| `TURN_START` | `{iteration}` | Each loop iteration begins |
| `TEXT_DELTA` | `{text}` | Model produces text |
| `TOOL_START` | `{name, id, args}` | Model requests a tool call |
| `TOOL_END` | `{name, id, result, is_error}` | Tool execution complete |
| `TURN_END` | `{iteration}` | Loop iteration ends |
| `COMPLETE` | `{text, session_id, cost_usd, usage}` | Final response |
| `ERROR` | `{message}` | Fatal error |

## Context Compaction

When conversation history approaches the model's context window:

1. **Find cut point** — walk backward keeping `keep_recent_tokens` (default 20K)
2. **Summarize** — send old messages to the model with a structured summary prompt
3. **Track file ops** — record which files were read/written/edited
4. **Restore files** — after compacting, re-read up to 5 recently accessed files (5K tokens each, 50K total budget)
5. **Deduplicate** — skip files already visible in the kept messages

### Tool Result Persistence

Results exceeding 50K characters are:
- Saved to disk: `~/.aloop/sessions/...tool_results/{id}.txt`
- Replaced in context with a 2K head preview + file path
- Exception: `read_file` results are never persisted (circular)

### Circuit Breaker

After 3 consecutive compaction failures, auto-compaction is disabled for the session. Prevents wasting API calls on irrecoverable context.

## File Access Model

Tools resolve paths relative to the project root. No restrictions by default — the agent can read and write anywhere under the project root.

Projects can add access controls via `before_tool` hooks:

```python
@hook("before_tool", priority=10)
def restrict_writes(name: str, args: dict, **ctx) -> dict:
    if name in ("write_file", "edit_file"):
        path = args.get("path", "")
        if not path.startswith("data/"):
            return {"allow": False, "reason": "Writes restricted to data/"}
    return {"allow": True}
```

## Caching Strategy

The system prompt is designed for prefix caching (automatic on OpenRouter/Anthropic):

- System prompt is 100% static (no timestamps, no changing content)
- AGENTS.md is part of the system prompt (also static)
- Conversation history grows each turn — prefix matches, only new tokens pay full price
- Compaction is the only thing that busts the cache (unavoidable)

## Network

- **API**: OpenRouter (`https://openrouter.ai/api/v1/chat/completions`)
- **Auth**: `OPENROUTER_API_KEY` env var or `~/.aloop/credentials.json`
- **Retry**: 2 attempts with exponential backoff (1s, 2s), max 60s delay
- **Retryable status codes**: 429, 502, 503, 504
- **Timeouts**: Per-model (default 60s, configurable via `stream_timeout` on ModelConfig)

## Module Map

| Module | Responsibility | Dependencies |
|--------|---------------|-------------|
| `__init__.py` | Project root discovery, public API | — |
| `cli.py` | CLI entry point, ANSI terminal output | agent_backend, system_prompt, models, tools |
| `agent_backend.py` | Core loop: stream, tool execution, compaction | compaction, models, session, hooks, tools, types |
| `system_prompt.py` | Prompt builder (template + section modes) | — (reads files directly) |
| `hooks.py` | Hook discovery and execution | — (loads .aloop/hooks/ dynamically) |
| `compaction.py` | Context summarization, file restoration | models |
| `session.py` | Persistent session management | compaction |
| `providers.py` | Provider registry (OpenRouter, OpenAI, Ollama, etc.) | — |
| `models.py` | Model registry with cost tracking | — |
| `types.py` | Event types, result types | — |
| `tools_base.py` | ToolDef, ToolResult base classes | — |
| `backend.py` | InferenceBackend protocol | types |
| `tools/__init__.py` | Tool registration and presets | tools_base, files, shell, skills |
| `tools/files.py` | read_file, write_file, edit_file | tools_base |
| `tools/shell.py` | bash tool | tools_base |
| `tools/skills.py` | load_skill tool, skill discovery | tools_base |
| `acp.py` | ACP server (Agent Client Protocol) | agent_backend, system_prompt, tools, types |
