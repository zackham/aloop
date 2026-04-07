# Architecture

## Overview

aloop is an agent loop harness — it sends prompts to language models, executes tool calls, and loops until the model produces a final text response.

```
┌─────────────────────────────────────────────────────────────┐
│                        aloop                                │
│                                                             │
│   System Prompt ──► Agent Loop ──► LLM Provider API         │
│   (.aloop/config)       │          (any model)              │
│                         │                                   │
│                    ┌────▼────┐                              │
│                    │  Tools  │                              │
│                    │ ┌─────┐ │  ┌──────────┐               │
│                    │ │files│ │  │  Hooks   │               │
│                    │ │bash │ │  │(.aloop/) │               │
│                    │ │grep │ │  └──────────┘               │
│                    │ │find │ │  ┌──────────┐               │
│                    │ │ls   │ │  │Permissions│              │
│                    │ │skill│ │  │(.aloop/)  │              │
│                    │ └─────┘ │  └──────────┘               │
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
   ├── Load config: ~/.aloop/config.json deep-merged with .aloop/config.json
   ├── Find instructions: ALOOP.md > AGENTS.md > .agents/AGENTS.md > CLAUDE.md > .claude/CLAUDE.md
   ├── Find skills: .aloop/skills/ ∪ .agents/skills/ ∪ .claude/skills/ ∪ ~/.aloop/skills/
   ├── Template mode: read ALOOP-PROMPT.md, interpolate {{variables}}
   └── Section mode: assemble defaults + overrides + instructions

2. Assemble messages
   ├── System prompt (position 0, cached by prefix matching)
   ├── Session history (if resuming)
   └── New user prompt

3. Run hooks (global ~/.aloop/hooks/ first, then project .aloop/hooks/)
   ├── gather_context → append to system prompt
   └── register_tools → extend tool set

4. Send to provider
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
   └── No tool calls → LOOP_END (exit loop)

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

Forked session:
  messages = parent chain walk (recursive) + own messages → add prompt → send → save

Stale session (>4h or >100 messages):
  auto-clear → treated as fresh
```

### Session Forking

Sessions support branching at any turn boundary via parent pointers:

```
Parent: [t1] [t2] [t3] [t4]
                 ↑
Child:   [t1] [t2] ← from parent (resolved on load)
                     [t5] [t6] ← child's own messages
```

Key properties:
- **Parent pointers, not copies** — child stores only `fork_from` + `fork_turn_id`, no message duplication
- **Recursive chain walk** — nested forks resolve by walking the parent chain (auto-materialize at depth 10)
- **Turn-boundary only** — fork points are after complete turns, never mid-tool-chain
- **Immutable prefix** — parent messages up to fork point are treated as immutable by the child
- **Compaction-safe** — children are materialized before parent compaction
- **`materialize()`** — flattens chain into standalone session, severs parent dependency

See [Sessions & Forking](SESSIONS.md) for full details.

### Subagents

A mode opts in to spawning child agents via `spawnable_modes` (allowlist of fresh-path targets) and/or `can_fork: true`. When opted in, the backend auto-injects the built-in `agent` tool, which delegates work via two paths:

- **Fork** — child inherits the parent's full conversation via session forking (`fork_from` + `fork_at`). Reuses the parent's `ALoop` instance; token usage rolls up into the parent.
- **Fresh** — child runs in a new `ALoop` instance with the target mode's config (model, system prompt, tools, permissions). Independent counters; brief it fully because it has no prior context.

Spawning is mediated by the `AgentExecutor` protocol; v0.6.0 ships `InProcessExecutor` as the sole implementation. Both paths persist `spawn_metadata` onto the child session for lineage tracking.

See [Subagents](SUBAGENTS.md) for the full model.

## Streaming Protocol

All events are `InferenceEvent(type, data, timestamp, session_id, turn_id, tool_call_id)`:

| Event | Data | When |
|-------|------|------|
| `LOOP_START` | `{session_id, model, provider}` | Loop begins (before first turn) |
| `TURN_START` | `{iteration, turn_id}` | Each loop iteration begins |
| `TEXT_DELTA` | `{text}` | Model produces text |
| `THINKING_DELTA` | `{text}` | Model thinking/reasoning |
| `TOOL_START` | `{name, id, args}` | Model requests a tool call |
| `TOOL_DELTA` | `{name, id, output}` | Streaming tool output (bash) |
| `TOOL_END` | `{name, id, result, is_error}` | Tool execution complete |
| `TURN_END` | `{iteration, turn_id, input_tokens, output_tokens, cost_usd}` | Loop iteration ends |
| `COMPACTION` | `{messages_before, messages_after, tokens_saved}` | Context compacted |
| `LOOP_END` | `{text, session_id, input_tokens, output_tokens, cost_usd, model, turns}` | Final response |
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

- **API**: Provider-specific endpoint (e.g. `https://openrouter.ai/api/v1/chat/completions` for OpenRouter)
- **Auth**: Provider-specific env var (e.g. `OPENROUTER_API_KEY`) or `~/.aloop/credentials.json`
- **Retry**: 2 attempts with exponential backoff (1s, 2s), max 60s delay
- **Retryable status codes**: 429, 502, 503, 504
- **Timeouts**: Per-model (default 60s, configurable via `stream_timeout` on ModelConfig)

## Module Map

| Module | Responsibility | Dependencies |
|--------|---------------|-------------|
| `__init__.py` | Project root discovery, public API | — |
| `cli.py` | CLI entry point, ANSI terminal output, config validation | agent_backend, system_prompt, models, tools, utils |
| `agent_backend.py` | ALoop class: stream, tool execution, compaction | config, compaction, models, session, hooks, tools, types |
| `config.py` | LoopConfig dataclass, mode resolution | compaction |
| `system_prompt.py` | Prompt builder (template + section modes), config loading, instruction/skill discovery | utils |
| `hooks.py` | Hook discovery and execution (global + project) | system_prompt (for config) |
| `compaction.py` | Context summarization, file restoration | models, utils |
| `session.py` | Persistent sessions, forking, materialization, GC, spawn metadata | compaction |
| `executor.py` | `AgentExecutor` protocol, `InProcessExecutor`, `AgentExecutionHandle` — spawns fork/fresh subagents | session, agent_result, types |
| `agent_result.py` | `AgentResult` dataclass, `FORK_BOILERPLATE`, `extract_partial_result` | — |
| `providers.py` | Provider registry (OpenRouter, OpenAI, Ollama, etc.) | utils |
| `models.py` | Model registry with cost tracking | utils |
| `utils.py` | JSONC parsing (strip_json_comments, load_jsonc) | — |
| `types.py` | Event types, result types | — |
| `tools_base.py` | ToolDef, ToolResult base classes | — |
| `backend.py` | InferenceBackend protocol | types |
| `tools/__init__.py` | Tool registration and presets | tools_base, files, shell, skills |
| `tools/files.py` | read_file, write_file, edit_file | tools_base |
| `tools/shell.py` | bash tool | tools_base |
| `tools/skills.py` | load_skill tool, skill discovery (global + project, merged) | tools_base, system_prompt |
| `tools/agent.py` | `build_agent_tool` factory — auto-injected agent tool with dynamic mode listing | tools_base, agent_result, executor |
| `acp.py` | ACP server (Agent Client Protocol) | agent_backend, system_prompt, tools, types |
