# aloop

A provider-agnostic, embeddable agent loop. Use any LLM from any provider, extend through hooks, embed as a Python library, drive from the CLI, or expose as an ACP server.

[Embedding](docs/EMBEDDING.md) · [CLI](docs/CLI.md) · [ACP](docs/ACP.md) · [Hooks](docs/HOOKS.md) · [System Prompt](docs/SYSTEM-PROMPT.md) · [Compaction](docs/COMPACTION.md) · [File Resolution](docs/FILE-RESOLUTION.md) · [Config](docs/CONFIG.md) · [Architecture](docs/ARCHITECTURE.md)

---

```python
from aloop import ALoop, EventType

loop = ALoop(model="x-ai/grok-4.1-fast")

async for event in loop.stream("Read README.md and summarize it"):
    if event.type == EventType.TEXT_DELTA:
        print(event.data["text"], end="")
```

```bash
aloop "What files are in this directory?"
```

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

- **Embedding agents in your product?** Python API, CLI, or ACP — pick the integration that fits. Five lines to a streaming agent with tools and sessions.
- **Outgrown vendor-locked agents?** Any model, any provider, domain-specific tools via hooks, headless automation, editor integration.
- **Multi-step AI workflows?** First-class [ACP](https://agentclientprotocol.com) step executor for [Stepwise](https://github.com/zackham/stepwise) and any ACP orchestrator.

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

See [docs/HOOKS.md](docs/HOOKS.md).

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

### Sessions with automatic compaction

Persistent sessions with context summarization, file restoration, and circuit breaker. Resume with `-c` or `--resume ID`.

### ACP server

`aloop serve` speaks [ACP](https://agentclientprotocol.com) over stdio — works with acpx, Zed, JetBrains, Neovim, Stepwise.

```bash
aloop register-acpx && acpx aloop "refactor the auth module"
```

## Install

```bash
uv tool install git+https://github.com/zackham/aloop.git
```

Requires Python 3.12+ and an API key:

```bash
export ALOOP_MODEL="x-ai/grok-4.1-fast"
export OPENROUTER_API_KEY="sk-or-..."
```

Or just run `aloop` — it prompts for your key on first use. Run `aloop init` to scaffold project config.

## Documentation

| Guide | What it covers |
|-------|---------------|
| **[Embedding](docs/EMBEDDING.md)** | Python API, `ALoop`, `stream()`, events, tools, sessions, providers |
| **[CLI](docs/CLI.md)** | Subcommands, flags, output formats, scripting |
| **[ACP](docs/ACP.md)** | acpx, Stepwise, editors, protocol details, modes |
| **[Hooks](docs/HOOKS.md)** | 10 hook points, `@tool` decorator, `ToolRejected`, execution order |
| **[System Prompt](docs/SYSTEM-PROMPT.md)** | Full prompt text, template mode, section overrides |
| **[Compaction](docs/COMPACTION.md)** | Context summarization, file restoration, circuit breaker |
| **[File Resolution](docs/FILE-RESOLUTION.md)** | Discovery chains, global/project layering, merge rules |
| **[Config](docs/CONFIG.md)** | JSONC config, modes, global/project layering |
| **[AGENTS.md](docs/AGENTS-MD.md)** | Project instruction file convention |
| **[Architecture](docs/ARCHITECTURE.md)** | Data flow, compaction, module map |

## License

MIT
