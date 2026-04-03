# aloop

A model-agnostic, embeddable agent loop. Use any LLM, extend through hooks, embed as a Python library or expose as an ACP server. No vendor lock-in, no framework opinions.

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

**[Quick Start](#install)** · **[Docs](docs/)** · **[Hooks](docs/HOOKS.md)** · **[Config](docs/CONFIG.md)** · **[Architecture](docs/ARCHITECTURE.md)** · **[Changelog](CHANGELOG.md)**

## Why aloop?

|  | Claude Code | Codex CLI | Aider | LangChain | **aloop** |
|---|---|---|---|---|---|
| Embeddable as a library | No | No | No | Yes (heavy) | **Yes (lightweight)** |
| Model-agnostic | No | No | Partial | Yes | **Yes — 5 tested providers** |
| Custom tools without forking | No | No | No | Framework abstractions | **Project-local hooks** |
| ACP interop (editors, orchestrators) | Native | Via adapter | No | No | **Built-in** |
| Opinionated about your workflow | Very | Very | Very | Very | **Minimal** |

aloop is to agent loops what Flask is to web frameworks: small, embeddable, and extensible through your project — not the library.

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

- **[AGENTS.md convention](docs/AGENTS-MD.md)** — project-specific instructions for the agent
- **[Configuration](docs/CONFIG.md)** — `.aloop/config.json` schema, system prompt modes, model aliases, compaction tuning
- **[Hooks](docs/HOOKS.md)** — extension points, `@hook` decorator, examples
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
