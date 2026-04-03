# aloop

An embeddable agent loop for integrating LLM agents into your software projects.

aloop is a Python agent harness designed to be embedded in applications, pipelines, and orchestration systems. It provides a core agent loop with built-in tools (file I/O, shell, skills), persistent sessions with context compaction, a hook system for project-specific extensions, and an [ACP](https://agentclientprotocol.com) server for interop with editors and orchestrators. Use it as a library via the Python API, drive it from the CLI, or connect it to any ACP-compatible client.

The harness is model-agnostic and project-independent. Projects define their identity via `AGENTS.md` files and configure behavior via `.aloop/config.json` and hooks. Ships with a provider registry supporting [OpenRouter](https://openrouter.ai), OpenAI, Anthropic, Google (Gemini), and Groq (all tested), plus Together AI and Ollama (community). Custom providers can be added via config.

## Install

```bash
# Global install (recommended)
uv tool install git+https://github.com/zackham/aloop.git

# Or from a local clone
git clone https://github.com/zackham/aloop.git
cd aloop
uv tool install .
```

Requires Python 3.12+ and an [OpenRouter API key](https://openrouter.ai/keys):

```bash
export OPENROUTER_API_KEY="sk-or-..."
export ALOOP_MODEL="x-ai/grok-4.1-fast"  # or any OpenRouter model ID
```

Or just run `aloop` — it will prompt you to paste your API key on first use.

For ACP integration (editors, acpx, Stepwise), also run:

```bash
aloop register-acpx
```

## Quick Start

```bash
# Set your default model (any OpenRouter model ID works)
export ALOOP_MODEL="x-ai/grok-4.1-fast"

# Interactive session (auto-created, drops into REPL)
aloop "What files are in this directory?"

# One-shot mode — print response and exit
aloop -p "Explain this codebase"

# Continue last session
aloop -c

# Resume a specific session by ID
aloop --resume abc123def456 "Pick up where we left off"

# Pipe input (implies -p)
echo "What is 2+2?" | aloop

# NDJSON streaming for automation
aloop -p --output-format stream-json "List all files"

# JSON result (final output only)
aloop -p --output-format json "Summarize README.md"
```

## How It Works

```
                    ┌──────────────────────┐
                    │   System Prompt      │
                    │ (from .aloop/config  │
                    │  or section defaults) │
                    └──────────┬───────────┘
                               │
User Prompt ──► Agent Loop ────┤
                    │          │
                    │    ┌─────▼─────┐
                    │    │  OpenRouter│
                    │    │  API Call  │
                    │    └─────┬─────┘
                    │          │
                    │    Text? ──► Stream to terminal
                    │          │
                    │    Tools? ──► Execute ──► Feed results back ──► Loop
                    │          │
                    │    Done?  ──► Return with usage stats
                    │          │
                    └──────────┘
```

Each turn: the model responds with text and/or tool calls. Tool results are fed back. The loop continues until the model responds with text only (no tool calls) or hits the iteration limit. Sessions persist to disk for multi-turn conversations. Long sessions are automatically compacted.

## Features

**Core Loop**
- Streaming text + tool call output to terminal
- Parallel tool execution when independent
- Per-model timeouts (no 5-minute hangs)
- Empty response handling, error recovery
- Circuit breaker on compaction failures (3 strikes)

**Built-in Tools**
- `read_file` — Read file contents (with line offsets)
- `write_file` — Create or overwrite files
- `edit_file` — Find & replace with smart quote normalization
- `bash` — Execute shell commands (timeout-enforced)
- `load_skill` — Load skill instructions on demand

**Skills**
- Skills are markdown files in `.agents/skills/` or `.claude/skills/`
- Short descriptions listed in system prompt (~2K tokens for all skills)
- Full content loaded on-demand via `load_skill` tool
- Progressive loading pattern (low context overhead)

**Hooks (Extensibility)**
- `before_tool` — Block, allow, or modify tool calls
- `after_tool` — Transform tool results
- `gather_context` — Inject task-specific context
- `register_tools` — Add project-specific tools
- Python `@hook` decorator with priority ordering
- See [docs/HOOKS.md](docs/HOOKS.md)

**Sessions & Compaction**
- Auto-created persistent sessions (resume with `-c` or `--resume ID`)
- Named sessions via `-s name` for memorable IDs
- Auto-compaction when context approaches model limits
- Post-compaction file restoration (re-reads recently accessed files)
- Stale session auto-clearing (4h / 100 messages)

**ACP & Integration**
- Built-in [ACP](https://agentclientprotocol.com) server (`aloop --acp`) for editor and orchestrator integration
- Works with any ACP client: [acpx](https://github.com/openclaw/acpx), Zed, JetBrains, Neovim
- Use as a step executor in [Stepwise](https://github.com/zackham/stepwise) flows
- Register with acpx in one command: `aloop register-acpx`

## Integration

### acpx / Stepwise

Register aloop as an ACP agent:

```bash
aloop register-acpx
```

Then use it from [acpx](https://github.com/openclaw/acpx) directly:

```bash
# Model via --model flag
acpx --model x-ai/grok-4.1-fast aloop "refactor the auth module"

# Or set a default
export ALOOP_MODEL="x-ai/grok-4.1-fast"
acpx aloop "refactor the auth module"
```

As a step executor in [Stepwise](https://github.com/zackham/stepwise) flows:

```yaml
steps:
  implement:
    executor: agent
    agent: aloop
    working_dir: ~/work/my-project
    prompt: "Implement feature X based on the spec"
    output_mode: stream_result
    permissions: approve_all
```

Set `ALOOP_MODEL` in the environment where Stepwise runs — it propagates to agent subprocesses automatically.

### Python API

```python
from aloop import AgentLoopBackend, EventType

backend = AgentLoopBackend(model="x-ai/grok-4.1-fast", api_key="sk-or-...")

# Streaming
async for event in backend.stream("Read README.md and summarize it"):
    if event.type == EventType.TEXT_DELTA:
        print(event.data["text"], end="")
    elif event.type == EventType.COMPLETE:
        print(f"\nCost: ${event.data['cost_usd']:.4f}")

# One-shot
result = await backend.run("What is 2+2?")
print(result.text)
```

## Project Setup

aloop discovers project configuration from the current working directory.

### AGENTS.md

The `AGENTS.md` file (or `CLAUDE.md`) provides project-specific instructions to the agent. aloop checks for these files in order:

1. `AGENTS.md`
2. `.agents/AGENTS.md`
3. `CLAUDE.md`
4. `.claude/CLAUDE.md`

This is the same convention used by Claude Code, Codex, and other agent tools. See [docs/AGENTS-MD.md](docs/AGENTS-MD.md) for details on what to put in it.

### .aloop/ directory

```
.aloop/
  config.json          # Harness configuration
  hooks/               # Python hook files (optional)
    __init__.py         # Hook decorator + discovery
    permissions.py      # Example: tool access control
    firebreaks.py       # Example: dangerous action checks
```

### ALOOP-PROMPT.md (optional)

A system prompt template that replaces the default section-based prompt. Write the entire prompt in your project's voice with `{{tools}}`, `{{skills}}`, and `{{agents_md}}` placeholders:

```markdown
i'm the digital assistant for this project.

## tools
{{tools}}

## skills
{{skills}}

## project details
{{agents_md}}
```

Reference it in config: `{"system_prompt": "file:ALOOP-PROMPT.md"}`

## Configuration

`.aloop/config.json`:

```jsonc
// Template mode — full control over the system prompt
{
  "system_prompt": "file:ALOOP-PROMPT.md"
}
```

```jsonc
// Section mode — override individual defaults
{
  "sections": {
    "preamble": false,           // omit this section
    "identity": false,           // omit (AGENTS.md handles it)
    "communication": "Be concise and technical.",  // replace default
    "actions": false             // omit
  }
}
```

Section mode assembles from defaults: `preamble`, `tools`, `skills`, `mechanics`, `task_approach`, `actions`, `communication`, `identity`, then appends AGENTS.md body under `# Project Context`.

See [docs/CONFIG.md](docs/CONFIG.md) for full schema reference.

## Built-in Tools

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `read_file` | Read file contents | `path`, `offset`, `limit` |
| `write_file` | Create or overwrite a file | `path`, `content` |
| `edit_file` | Find & replace text | `path`, `old_string`, `new_string` |
| `bash` | Execute a shell command | `command`, `timeout` |
| `load_skill` | Load a skill's SKILL.md | `skill`, `args` |

File tools resolve paths relative to the project root. No restrictions by default — projects can add access controls via `before_tool` hooks.

## Hooks

Hooks are Python files in `.aloop/hooks/` with `@hook` decorators:

```python
# .aloop/hooks/safety.py
from aloop_hooks import hook

@hook("before_tool", priority=10)
def block_dangerous(name: str, args: dict, **ctx) -> dict:
    if name == "bash" and "rm -rf" in args.get("command", ""):
        return {"allow": False, "reason": "Blocked: destructive command"}
    return {"allow": True}
```

| Extension Point | Signature | Returns |
|----------------|-----------|---------|
| `before_tool` | `(name, args, **ctx)` | `{"allow": bool, "reason": str, "modified_args": dict}` |
| `after_tool` | `(name, args, result, **ctx)` | `{"modified_result": str}` |
| `gather_context` | `(**kwargs)` | `str` (appended to system prompt) |
| `register_tools` | `()` | `list[ToolDef]` |

Lower priority numbers run first. All hooks are optional — the harness works without any. See [docs/HOOKS.md](docs/HOOKS.md).

## Providers & Models

aloop ships with a registry of API providers. OpenRouter is the default (any model on the platform). Switch providers with `--provider`:

```bash
# OpenRouter (default) — any model ID
aloop --model x-ai/grok-4.1-fast "summarize this file"

# Direct OpenAI
aloop --provider openai --model gpt-4o "refactor this function"

# Local Ollama (no key needed)
aloop --provider ollama --model llama3 "explain this codebase"
```

See all providers: `aloop list-providers`
Test a provider: `aloop validate-provider --provider openai --model gpt-4o-mini`

Set defaults via environment or config:

```bash
export ALOOP_MODEL="x-ai/grok-4.1-fast"
```

Or in `~/.aloop/config.json`:

```json
{"provider": "openai"}
```

Add custom providers via `~/.aloop/providers.json`:

```json
{
  "my-corp": {
    "name": "Internal LLM",
    "base_url": "https://llm.internal.corp/v1/chat/completions",
    "env_key": "CORP_LLM_KEY"
  }
}
```

## System Prompt

Two modes:

**Template mode** — `.aloop/config.json` has `"system_prompt": "file:ALOOP-PROMPT.md"`. The template IS the prompt. Variables `{{tools}}`, `{{skills}}`, `{{agents_md}}` are interpolated.

**Section mode** — No `system_prompt` key. Harness assembles from defaults (preamble, tools, skills, mechanics, task_approach, actions, communication, identity) + AGENTS.md body.

Inspect the current prompt:

```bash
aloop system-prompt                # show template (raw)
aloop system-prompt --rendered     # show with variables interpolated
```

## CLI Reference

```
aloop [PROMPT] [OPTIONS]

Options:
  --model, -m MODEL          Model ID (or set ALOOP_MODEL)
  --provider NAME            API provider (default: openrouter)
  -p                         One-shot: print response and exit (no REPL)
  -c, --continue             Continue last session
  --resume SESSION_ID        Resume a specific session by ID
  -s, --session KEY          Named session (instead of auto-generated ID)
  -o, --output-format FMT    text (default), json, or stream-json
  --tools NAMES              Comma-separated tool names
  --no-context               Skip context injection from hooks
  --max-iterations N         Max loop iterations (default: 50)
  --acp                      Run as ACP server over stdio
  --version                  Show version and exit
  --list-models              List registered model aliases

Subcommands:
  aloop list-providers           List available API providers
  aloop validate-provider        Test a provider's API compatibility
  aloop system-prompt            Show the system prompt template
  aloop system-prompt --rendered Show fully interpolated prompt
  aloop update                   Self-update to latest version
  aloop register-acpx            Register aloop with acpx for ACP integration
```

Sessions are auto-created on every invocation. The session ID is printed when exiting the REPL or with `-p` mode. Use `-s name` to give a session a memorable name (e.g. `aloop -s refactor`), `--resume ID` to return to a specific session, or `-c` to continue the most recent one.

## Architecture

```
src/aloop/
  __init__.py          Project root discovery, public API
  cli.py               CLI entry point (aloop command)
  agent_backend.py     Core agent loop (OpenRouter streaming)
  system_prompt.py     System prompt builder (template + section modes)
  hooks.py             Hook discovery and execution
  compaction.py        Context compaction with circuit breaker
  session.py           Persistent session management
  providers.py         Provider registry (OpenRouter, OpenAI, Ollama, etc.)
  models.py            Model alias registry with cost tracking
  types.py             Event types and streaming protocol
  tools_base.py        ToolDef, ToolResult base classes
  backend.py           InferenceBackend protocol
  acp.py               ACP server (Agent Client Protocol)
  tools/
    __init__.py        Tool registration and presets
    files.py           read_file, write_file, edit_file
    shell.py           bash tool
    skills.py          load_skill tool + skill discovery
```

## Contributing

aloop is designed to be project-independent — any project with an `AGENTS.md` and optional `.aloop/` config works.

To add a tool: create a `ToolDef` in `src/aloop/tools/` and register it in `tools/__init__.py`.
To add a hook: create a `.py` file in `.aloop/hooks/` with `@hook` decorators.
To add a model alias: add to `~/.aloop/models.json` (or pass any OpenRouter model ID directly).

## License

MIT
