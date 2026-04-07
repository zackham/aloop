# aloop

An embeddable Python agent loop for **multi-agent orchestration**, **declarative permissions**, and **editor integration via ACP**. Use any LLM from any provider, extend through hooks, embed as a library, drive from the CLI, or expose as an ACP server.

## Quickstart

```bash
uv tool install git+https://github.com/zackham/aloop.git
aloop --model x-ai/grok-4.1-fast "what files are in this directory?"
```

## Documentation

**Usage**
- [CLI Reference](docs/CLI.md) — subcommands, flags, output formats, scripting
- [ACP Integration](docs/ACP.md) — acpx, Stepwise, editors, protocol details
- [Embedding Guide](docs/EMBEDDING.md) — Python API, `ALoop`, `stream()`, events, tools, sessions

**Configuration**
- [Config](docs/CONFIG.md) — JSONC config, modes, global/project layering
- [Hooks](docs/HOOKS.md) — 10 hook points, `@tool` decorator, `ToolRejected`, execution order
- [Permissions](docs/PERMISSIONS.md) — tool sets, path restrictions, readonly modes, security model
- [System Prompt](docs/SYSTEM-PROMPT.md) — full prompt transparency, template mode, section overrides
- [AGENTS.md](docs/AGENTS-MD.md) — project instruction file convention

**Internals**
- [Architecture](docs/ARCHITECTURE.md) — data flow, module map, event protocol
- [Sessions & Forking](docs/SESSIONS.md) — persistence, branching, materialization, garbage collection
- [Subagents](docs/SUBAGENTS.md) — fork & fresh spawn paths, `agent` tool, executor protocol
- [File Resolution](docs/FILE-RESOLUTION.md) — discovery chains, global/project layering, merge rules
- [Compaction](docs/COMPACTION.md) — context summarization, file restoration, circuit breaker

## What aloop is for

aloop is a **library, not an interactive coding agent**. It's optimized for three specific use cases:

1. **Multi-agent orchestration with structural safety.** Spawn child agents via the built-in `agent` tool, with two paths: **fork** (child inherits parent context via session forking, shares prompt cache, can recurse) and **fresh** (child runs a clean session with a different mode's model, prompt, tools, and permissions). Permission escalation is prevented structurally via per-mode `spawnable_modes` allowlists — a read-only mode literally cannot name a write-capable mode in its allowlist. No runtime permission checks, no escalation paths. See [Subagents](docs/SUBAGENTS.md).

2. **Declarative permissions.** Tool sets and path restrictions live in `.aloop/config.json` — auditable, transparent, enforced before tool execution. Modes can swap entire tool sets (`READONLY_TOOLS`, `CODING_TOOLS`, custom). Path globs deny or allow file operations. Hardcoded safety net catches catastrophic commands. See [Permissions](docs/PERMISSIONS.md).

3. **Editor integration via ACP.** `aloop serve` speaks the [Agent Client Protocol](https://agentclientprotocol.com) over stdio — works directly with acpx, Zed, JetBrains, Neovim, and orchestrators like [Stepwise](https://github.com/zackham/stepwise). No custom adapters needed. See [ACP](docs/ACP.md).

If your use case is one of these, aloop is built for it. If it's something else, the next section will help you find a better tool.

## What aloop is NOT

**Not an interactive terminal coding agent.** No rich TUI, no `/tree` session navigator, no inline editor, no theming. The CLI exists for scripting and ACP serving. If you want a polished interactive terminal experience, [pi-mono](https://github.com/badlogic/pi-mono) is the mature choice — it targets a different problem than aloop and the two are complementary.

**Not a complete product.** No UI, no telemetry, no billing, no marketplace. aloop is a small library you embed and extend.

**Not a high-level "agent framework."** No agent classes to subclass, no orchestration DSL, no pre-baked patterns. It's an agent loop with a clean API and good extension points. You build your own abstractions on top.

## Comparison

|  | **aloop** | [pi-mono](https://github.com/badlogic/pi-mono) | Claude Agent SDK | OpenAI Agents SDK |
|---|---|---|---|---|
| **Built-in subagents** | **Yes — fork + fresh paths, recursive, structural permissions** | No (extension example only) | No | No |
| **Declarative permissions** | **Yes — config-level tool sets + path restrictions** | No (extensions handle gating) | No | No |
| **ACP server** | **Built-in** | No | Community adapter | Community adapter |
| Interactive TUI | **No (use pi-mono)** | **Yes — rich, polished, mature** | No (terminal product) | No |
| Provider count | 5 tested + any OpenAI-compatible | **18+, including CLI subscriptions** | Anthropic only | OpenAI-native |
| Language | Python | TypeScript/Bun | Python/TypeScript | Python/TypeScript |
| Codebase size | ~8.5K LOC | ~74K LOC | Full product | Framework |
| Custom tools | `@tool` decorator + hooks | TypeScript extensions | MCP + plugins + hooks | Functions + MCP |
| System prompt control | **Full — defaults [published](docs/SYSTEM-PROMPT.md)** | Override via SYSTEM.md | Appendable | Append via AGENTS.md |
| Maturity | v0.6.0 (recent) | v0.65.x (mature) | Stable | Stable |

aloop is smaller and more focused than the alternatives. The three things in bold are where it does work the others don't.

## Key Features

### Multi-agent orchestration with structural safety

Modes opt in to spawning via `spawnable_modes` (allowlist of mode names this mode can spawn) and `can_fork` (whether the fork path is allowed). The `agent` tool gets auto-injected into the mode's tool set. Spawned children persist `spawn_metadata` for full lineage tracking — visible via `aloop sessions info <id>`. Recursive forking is supported (an aloop differentiator vs Claude Code's cache-coherency-driven `fork→fork` block, which doesn't apply to aloop's session-based model). See [Subagents](docs/SUBAGENTS.md).

```jsonc
{
  "modes": {
    "orchestrator": {
      "tools": ["read_file", "grep", "find", "ls"],
      "spawnable_modes": ["explore", "worker", "reviewer"],
      "can_fork": true
    },
    "explore": {
      "tools": ["read_file", "grep", "find", "ls"],
      "subagent_eligible": true,
      "spawnable_modes": ["explore"]
    },
    "worker": {
      "tools": ["read_file", "write_file", "edit_file", "bash"],
      "subagent_eligible": true,
      "spawnable_modes": ["explore"]
    },
    "reviewer": {
      "tools": ["read_file", "grep"],
      "subagent_eligible": true
    }
  }
}
```

A read-only mode cannot list a write-capable mode in its `spawnable_modes`. The escalation boundary is the config itself — auditable, structural, no runtime checks.

### Declarative permissions

Permissions live in config, not in code:

```jsonc
{
  "permissions": {
    "paths": {
      "deny": [".env", "**/*.key", "**/*.pem"],
      "allow_outside_project": false,
      "additional_dirs": ["~/work/shared-lib"]
    }
  },
  "modes": {
    "review": {
      "tools": ["read_file", "grep", "find", "ls"]
    },
    "implement": {
      "tools": ["*"],
      "permissions": {
        "paths": { "write": ["src/**", "tests/**"] }
      }
    }
  }
}
```

Tool sets are the primary security boundary. Path restrictions are enforced before tool execution. A hardcoded safety net catches catastrophic commands (`rm -rf /`, fork bombs, etc.) regardless of mode. See [Permissions](docs/PERMISSIONS.md).

### ACP server

`aloop serve` speaks [ACP](https://agentclientprotocol.com) over stdio — drop-in for acpx, Zed, JetBrains, Neovim, and Stepwise. This is the canonical path for rich UI with aloop:

```bash
aloop register-acpx && acpx aloop "refactor the auth module"
```

See [ACP](docs/ACP.md).

### Any model, any provider

5 tested providers (OpenRouter, OpenAI, Anthropic, Google, Groq) plus any OpenAI-compatible endpoint. Add custom providers in 4 lines of JSON, validate with `aloop providers validate`.

```bash
aloop --model x-ai/grok-4.1-fast "refactor this"
aloop --provider openai --model gpt-4o "explain this"
aloop --provider ollama --model llama3 "summarize this"
```

### Hooks — extend without forking the library

10 hook points in `.aloop/hooks/` — lifecycle, tools, context, compaction. Global (`~/.aloop/hooks/`) and project-local, both run. Add tools via the `@tool` decorator:

```python
from aloop import tool, ToolParam
from typing import Annotated

@tool(name="query_db", description="Run a SQL query")
async def query_db(
    sql: Annotated[str, ToolParam(description="SQL query")],
) -> str:
    return str(await db.execute(sql))
```

See [Hooks](docs/HOOKS.md).

### Full system prompt control

No hidden instructions. Write your own prompt (template mode) or override individual sections. The [full default prompt is published](docs/SYSTEM-PROMPT.md):

```bash
aloop system-prompt --rendered  # see exactly what the model receives
```

### Sessions with forking and compaction

Persistent sessions with turn-boundary forking via parent pointers (no message duplication on disk), recursive chain walk, depth-10 auto-materialize, context compaction with circuit breaker and post-compaction file restoration. Resume with `--continue` or `--resume ID`. See [Sessions & Forking](docs/SESSIONS.md) and [Compaction](docs/COMPACTION.md).

## License

MIT
