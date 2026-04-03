# aloop

A provider-agnostic, embeddable agent loop. Use any LLM from any provider, extend through hooks, embed as a Python library or expose as an ACP server. No vendor lock-in, no framework opinions.

[Embedding](docs/EMBEDDING.md) · [CLI](docs/CLI.md) · [ACP](docs/ACP.md) · [Hooks](docs/HOOKS.md) · [System Prompt](docs/SYSTEM-PROMPT.md) · [Config](docs/CONFIG.md) · [Architecture](docs/ARCHITECTURE.md)

---

```python
from aloop import AgentLoopBackend, EventType

backend = AgentLoopBackend(model="x-ai/grok-4.1-fast", api_key="sk-or-...")

async for event in backend.stream("Read README.md and summarize it"):
    if event.type == EventType.TEXT_DELTA:
        print(event.data["text"], end="")
```

Or from the command line:

```bash
aloop "What files are in this directory?"
```

## Why aloop?

|  | **aloop** | Claude Agent SDK | OpenAI Agents SDK | Pydantic AI | Google ADK |
|---|---|---|---|---|---|
| Provider-agnostic | **Yes — 5 tested** | Anthropic only | OpenAI-native (adapters for others) | Yes — many native | Google-optimized |
| Embeddable | **Python, async** | Python + TS | Python (TS via subprocess) | Python | Python |
| Custom tools | **Project-local hooks** | MCP + plugins + hooks | Functions + MCP | Decorators + MCP | Functions + MCP + OpenAPI |
| System prompt control | **Full — [published defaults](docs/SYSTEM-PROMPT.md)** | Replaceable in SDK | Append via AGENTS.md | Full | Full |
| ACP (editor/orchestrator interop) | **Built-in** | Via community adapter | Via community adapter | No | No (has A2A) |
| Weight | **~2500 LOC, 1 dep** | Full product runtime | Multi-agent framework | Type-safe framework | Google ecosystem toolkit |

aloop is to agent loops what Flask is to web frameworks: small, embeddable, and extensible through your project — not the library. The others bring more features but also more opinions, more dependencies, and more lock-in.

### Who is this for?

- **Building agent features into your product?** aloop gives you a production-ready loop you embed in 5 lines and extend through hooks. Not a subprocess you shell out to — an async iterator you consume in your own code.
- **Outgrown vendor-locked coding agents?** Use any model from any provider, add domain-specific tools, run headless in automation, integrate with your editor.
- **Building multi-step AI workflows?** aloop is a first-class ACP step executor for [Stepwise](https://github.com/zackham/stepwise) and any orchestrator that speaks [ACP](https://agentclientprotocol.com).

## Key Features

### Any model, any provider

5 tested providers (OpenRouter, OpenAI, Anthropic, Google, Groq), plus community support for Together AI and Ollama. Custom providers in 4 lines of JSON. Switch models with an env var.

```bash
aloop --provider openai --model gpt-4o "refactor this"
aloop --provider google --model gemini-2.5-flash "explain this"
aloop --provider ollama --model llama3 "summarize this"
```

### Hooks — extend without forking

Four extension points that live in your project's `.aloop/hooks/` directory — not in aloop's source:

```python
# .aloop/hooks/tools.py
from aloop_hooks import hook
from aloop import ToolDef, ToolResult

@hook("register_tools")
def my_tools():
    async def _query_db(sql: str) -> ToolResult:
        # your domain-specific tool
        result = await run_query(sql)
        return ToolResult(content=str(result))

    return [ToolDef(
        name="query_db",
        description="Run a SQL query against the analytics database",
        parameters={"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
        execute=_query_db,
    )]
```

Also: `before_tool` (block/modify), `after_tool` (transform results), `gather_context` (inject dynamic context). See [docs/HOOKS.md](docs/HOOKS.md).

### Sessions with automatic compaction

Long-running conversations don't crash or lose context. The compaction system summarizes old context, keeps recent messages, restores recently-accessed files, and has a circuit breaker on failures. Sessions persist to `~/.aloop/sessions/` and resume with `-c` or `--resume ID`.

### ACP server for editor and orchestrator interop

`aloop --acp` exposes the agent as an [Agent Client Protocol](https://agentclientprotocol.com) server. Works with acpx, Zed, JetBrains, Neovim, and Stepwise:

```bash
aloop register-acpx              # one-time setup
acpx aloop "refactor the auth module"  # use from acpx
```

```yaml
# Stepwise flow
steps:
  implement:
    executor: agent
    agent: aloop
    prompt: "Implement feature X"
```

### Full system prompt control

No hidden instructions. Two modes: write your own prompt from scratch (template mode with `{{tools}}`, `{{skills}}`, `{{agents_md}}` variables), or override individual sections of the defaults. Inspect exactly what the model sees at any time:

```bash
aloop system-prompt --rendered
```

The full default prompt is published in [docs/SYSTEM-PROMPT.md](docs/SYSTEM-PROMPT.md) — nothing is hidden.

### Built-in tools

`read_file`, `write_file`, `edit_file`, `bash`, `load_skill` — all with no access restrictions by default. Projects add controls via `before_tool` hooks.

## Install

```bash
# Global install
uv tool install git+https://github.com/zackham/aloop.git

# Or from a local clone
git clone https://github.com/zackham/aloop.git && cd aloop && uv tool install .
```

Requires Python 3.12+ and an API key for your provider:

```bash
export ALOOP_MODEL="x-ai/grok-4.1-fast"
export OPENROUTER_API_KEY="sk-or-..."  # or OPENAI_API_KEY, GOOGLE_API_KEY, etc.
```

Or just run `aloop` — it will prompt you to paste your API key on first use.

For ACP integration: `aloop register-acpx`

## CLI

```bash
aloop "prompt"                    # interactive session
aloop -p "prompt"                 # one-shot (print and exit)
aloop -c                          # continue last session
aloop --resume ID "prompt"        # resume specific session
aloop -s refactor "prompt"        # named session
aloop -o json -p "prompt"         # JSON output
aloop -o stream-json -p "prompt"  # NDJSON streaming
```

See `aloop --help` for all options, or `aloop list-providers` / `aloop validate-provider` for provider management.

### Commands

| Command | Description |
|---------|-------------|
| `aloop list-providers` | Show available API providers with status |
| `aloop validate-provider` | Test a provider's API compatibility |
| `aloop system-prompt` | Inspect the current system prompt |
| `aloop update` | Self-update to latest version |
| `aloop register-acpx` | Register with acpx for ACP integration |

## Project Setup

aloop discovers project configuration from the current working directory. See the dedicated docs for details:

- **[Embedding Guide](docs/EMBEDDING.md)** — Python API, events, custom tools, sessions, providers
- **[CLI Reference](docs/CLI.md)** — flags, output formats, scripting patterns
- **[ACP Integration](docs/ACP.md)** — acpx, Stepwise, editors, protocol details
- **[Hooks](docs/HOOKS.md)** — extension points, `@hook` decorator, examples
- **[System Prompt](docs/SYSTEM-PROMPT.md)** — full prompt transparency, template mode, overrides
- **[Config](docs/CONFIG.md)** — `.aloop/config.json` schema, compaction tuning
- **[AGENTS.md](docs/AGENTS-MD.md)** — project instruction convention
- **[Architecture](docs/ARCHITECTURE.md)** — data flow, compaction internals, module map

## Extending

All customization is external — no need to modify aloop source.

- **Add tools**: `register_tools` hook in `.aloop/hooks/`
- **Add skills**: drop a `SKILL.md` in `.agents/skills/your-skill/`
- **Control tool access**: `before_tool` / `after_tool` hooks
- **Inject context**: `gather_context` hooks
- **Add model aliases**: `~/.aloop/models.json`
- **Add providers**: `~/.aloop/providers.json`
- **Customize the prompt**: template mode (`ALOOP-PROMPT.md`) or section overrides

## License

MIT
