# aloop

Embeddable Python agent loop — any model, any provider, extend through hooks.

## Architecture

- `src/aloop/` — 18 Python modules
- `agent_backend.py` — ALoop class (core loop): stream completions, execute tools, compact context
- `config.py` — LoopConfig dataclass, mode resolution
- `utils.py` — JSONC parsing (strip_json_comments, load_jsonc)
- `providers.py` — provider registry (OpenRouter, OpenAI, Anthropic, Google, Groq + community)
- `cli.py` — CLI with subcommands, REPL, one-shot, JSON output, self-update, provider validation
- `acp.py` — ACP server for acpx/editor/orchestrator integration
- `system_prompt.py` — template mode (ALOOP-PROMPT.md) and section mode (defaults + overrides)
- `compaction.py` — context summarization with circuit breaker and file restoration
- `hooks.py` — 10 extension points via `.aloop/hooks/` Python files
- `tools_base.py` — ToolDef, ToolResult, ToolRejected, @tool decorator
- `tools/` — read_file, write_file, edit_file, bash, load_skill

## Build & Test

```bash
uv sync --extra dev
uv run pytest tests/ -q
```

## Key conventions

- Single version source: `src/aloop/__init__.py` (`__version__`), read by hatchling
- State lives in `~/.aloop/` (sessions, credentials, models, compaction, providers)
- Config files support JSONC (// and # comments)
- Project config lives in `.aloop/config.json` and `.aloop/hooks/`
- Skills merged from `.aloop/skills/` ∪ `.agents/skills/` ∪ `.claude/skills/` ∪ `~/.aloop/skills/`
- Named modes in config for different workflows (review, fast, etc.)
- No file access restrictions by default
- Any OpenAI-compatible API endpoint works as a provider

## When making changes

- Run `uv run pytest tests/ -q` before committing
- 342+ tests across 12 files
- Update CHANGELOG.md for any user-facing changes
- Update docs/ if changing config format, hooks API, or provider behavior
