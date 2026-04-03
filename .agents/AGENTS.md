# aloop

An embeddable agent loop for integrating LLM agents into software projects.

## What this is

aloop is a Python agent harness. It runs inference through OpenRouter, executes tools, manages sessions with compaction, and speaks ACP for editor/orchestrator integration. It's designed to be embedded in applications and pipelines, not to compete with interactive coding agent REPLs.

## Architecture

- `src/aloop/` — 16 Python modules, ~2500 LOC
- `agent_backend.py` — core loop: stream completions, execute tools, compact context
- `cli.py` — CLI entry point with REPL, one-shot, JSON output modes
- `acp.py` — ACP server for acpx/editor/orchestrator integration
- `system_prompt.py` — template mode (ALOOP-PROMPT.md) and section mode (defaults + overrides)
- `compaction.py` — context summarization with circuit breaker and file restoration
- `hooks.py` — extensibility via `.aloop/hooks/` Python files
- `tools/` — read_file, write_file, edit_file, bash, load_skill

## Build & Test

```bash
uv sync --extra dev
uv run pytest tests/ -v
```

## Key conventions

- Single version source: `src/aloop/__init__.py` (`__version__`), read by hatchling
- State lives in `~/.aloop/` (sessions, credentials, models, compaction config)
- Project config lives in `.aloop/config.json` and `.aloop/hooks/`
- Skills in `.agents/skills/` (falls back to `.claude/skills/`)
- No file access restrictions by default — projects add controls via `before_tool` hooks
- All OpenRouter model IDs work directly, no built-in model registry

## When making changes

- Run `uv run pytest tests/ -q` before committing
- 142 tests across 6 files (acp, agent_backend, cli, compaction, edit_file, skills)
- Use the `release` skill for version bumps: determines version, updates CHANGELOG.md, bumps __version__, tags, pushes
