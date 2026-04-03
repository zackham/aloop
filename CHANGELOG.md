# Changelog

All notable changes to aloop are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

## [0.2.0] - 2026-04-03

### Added
- Multi-provider support with tested registry: OpenRouter, OpenAI, Anthropic, Google (Gemini), Groq
- Community providers: Together AI, Ollama (local)
- `--provider` CLI flag to select API provider
- `aloop list-providers` to see available providers with status
- `aloop validate-provider` to test a provider's full API compatibility (streaming, tool calling, multi-turn)
- Custom providers via `~/.aloop/providers.json`
- Default provider configurable in `~/.aloop/config.json`
- Provider-aware credential management (per-provider env vars, shared credentials file)
- `aloop register-acpx` to register with acpx for ACP integration
- `aloop update` for self-updating from GitHub
- `-p` one-shot mode, `-c` continue last session, `--resume` by session ID
- `-s` named sessions for memorable IDs
- `--output-format json|stream-json` for scripting and automation
- `set_session_model` ACP support for per-session model switching via `acpx --model`
- Interactive API key setup on first run
- readline support in REPL (ctrl-a/e/w, history)
- CHANGELOG.md and release skill (`.agents/skills/release/`)
- `.agents/AGENTS.md` with CLAUDE.md symlinks for agent compatibility

### Changed
- Sessions auto-created on every invocation (no `--session` flag required)
- No built-in model registry — any model ID works directly with any provider
- No file access restrictions by default (projects add controls via hooks)
- No bash timeout cap (was 300s)
- All state under `~/.aloop/` (sessions, credentials, config)
- Removed `task_type` machinery (use constructor args and hooks instead)
- `load_session` ACP method always succeeds (creates fresh if not on disk)

## [0.1.0] - 2026-04-03

Initial release. Core agent loop, built-in tools, skill system, hook system, system prompt builder, persistent sessions with compaction, ACP server.
