# aloop

A provider-agnostic, embeddable agent loop. Use any LLM from any provider, extend through hooks, embed as a Python library, drive from the CLI, or expose as an ACP server.

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

## Why aloop?

|  | **aloop** | Claude Agent SDK | OpenAI Agents SDK | Pydantic AI | Google ADK |
|---|---|---|---|---|---|
| Provider-agnostic | **5 tested, custom via JSON** | Anthropic only | OpenAI-native | Many native | Google-optimized |
| Custom tools | **Hooks + `@tool` decorator** | MCP + plugins + hooks | Functions + MCP | Decorators + MCP | Functions + MCP + OpenAPI |
| System prompt control | **Full — [defaults published](docs/SYSTEM-PROMPT.md)** | Appendable (replaceable in SDK) | Append via AGENTS.md | Full | Full |
| ACP interop | **Built-in** | Community adapter | Community adapter | No | No |
| Footprint | **~5K LOC, 2 deps** | Full product runtime | Framework + abstractions | Framework + Pydantic | Google ecosystem |

Small, embeddable, extensible through your project — not the library.

### Who is this for?

- **Want full control over your agent?** System prompt, tools, context, compaction — everything is overridable, inspectable, and [documented](docs/SYSTEM-PROMPT.md). No black boxes.
- **Need an open, extensible foundation?** 10 hook points, `@tool` decorator, JSONC config, named modes. Extend through your project, not the library.
- **Integrating agents into your stack?** Python API, CLI, or [ACP](https://agentclientprotocol.com) — embed in your app, script from shell, or plug into editors and orchestrators like [Stepwise](https://github.com/zackham/stepwise).

## Key Features

### Any model, any provider

5 tested providers (OpenRouter, OpenAI, Anthropic, Google, Groq), plus any endpoint compatible with the OpenAI chat completions API. Add custom providers in 4 lines of JSON, validate with `aloop providers validate`.

```bash
aloop --model x-ai/grok-4.1-fast "refactor this"
aloop --provider openai --model gpt-4o "explain this"
aloop --provider ollama --model llama3 "summarize this"
```

### Hooks — extend without forking

10 hook points in `.aloop/hooks/` — lifecycle, tools, context, compaction. Global (`~/.aloop/hooks/`) + project-local, both run.

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

No hidden instructions. Write your own prompt (template mode) or override individual sections. The [full default prompt is published](docs/SYSTEM-PROMPT.md).

```bash
aloop system-prompt --rendered  # see exactly what the model receives
```

### Named modes

Different system prompts, tools, models, and compaction settings per workflow — switch via CLI, Python API, or ACP.

```bash
aloop --mode review "check the auth module"
```

### Sessions with forking and compaction

Persistent sessions with branching at any turn, context summarization, file restoration, and circuit breaker. Fork conversations for subagent patterns or edit+rerun workflows. Resume with `--continue` or `--resume ID`. See [Sessions & Forking](docs/SESSIONS.md) and [Compaction](docs/COMPACTION.md).

### Subagents

Modes can opt in to spawning child agents via the auto-injected `agent` tool. Two paths: **fork** (child inherits the parent's full conversation, shares the prompt cache) and **fresh** (child runs a clean session with a different mode's model, system prompt, and tools). `spawnable_modes` is the structural permission boundary. See [Subagents](docs/SUBAGENTS.md).

### ACP server

`aloop serve` speaks [ACP](https://agentclientprotocol.com) over stdio — works with acpx, Zed, JetBrains, Neovim, Stepwise. See [ACP](docs/ACP.md).

```bash
aloop register-acpx && acpx aloop "refactor the auth module"
```

## License

MIT
