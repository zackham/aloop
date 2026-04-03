# aloop

Embeddable Python agent loop — any model, any provider, extend through hooks.

## Architecture

- `src/aloop/` — 17 Python modules
- `agent_backend.py` — core loop: stream completions, execute tools, compact context
- `providers.py` — provider registry (OpenRouter, OpenAI, Anthropic, Google, Groq + community)
- `cli.py` — CLI with REPL, one-shot, JSON output, self-update, provider validation
- `acp.py` — ACP server for acpx/editor/orchestrator integration
- `system_prompt.py` — template mode (ALOOP-PROMPT.md) and section mode (defaults + overrides)
- `compaction.py` — context summarization with circuit breaker and file restoration
- `hooks.py` — extensibility via `.aloop/hooks/` Python files
- `tools/` — read_file, write_file, edit_file, bash, load_skill

## Build & Test

```bash
uv sync --extra dev
uv run pytest tests/ -q
```

## Key conventions

- Single version source: `src/aloop/__init__.py` (`__version__`), read by hatchling
- State lives in `~/.aloop/` (sessions, credentials, models, compaction, providers)
- Project config lives in `.aloop/config.json` and `.aloop/hooks/`
- Skills in `.agents/skills/` (falls back to `.claude/skills/`)
- No file access restrictions by default
- Any OpenAI-compatible API endpoint works as a provider
- Use the `release` skill for version bumps

## When making changes

- Run `uv run pytest tests/ -q` before committing
- 142 tests across 6 files (acp, agent_backend, cli, compaction, edit_file, skills)
- Update CHANGELOG.md for any user-facing changes
- Update docs/ if changing config format, hooks API, or provider behavior
