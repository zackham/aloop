# Changelog

All notable changes to aloop are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.5.0] - 2026-04-06

### Added
- **Session forking:** branch a conversation at any turn via parent pointers with recursive chain walk. Fork creates a lightweight child session referencing the parent — no message duplication on disk. Multiple forks from the same parent share the parent's messages.
- **Turn ID persistence:** every message now carries a `turn_id` field (12-char hex). All messages within a turn share the same ID. Persisted in both `context.json` and `log.jsonl`. Turn IDs are the addressing unit for forking.
- **`stream()` fork kwargs:** `fork_from` (parent session_id), `fork_at` (turn_id, defaults to last turn), `replace_turn` (edit+rerun — truncate and replace a turn in-place).
- **`AgentSession.fork()`:** classmethod to create a forked child session. Validates parent exists and turn_id is valid.
- **`AgentSession.resolve_messages()`:** walks the parent chain recursively, returning the full message history. Auto-materializes at depth 10.
- **`AgentSession.materialize()`:** flattens the fork chain into a standalone session, severing parent dependency.
- **`AgentSession.children()`:** scans session directory for sessions forked from this one.
- **`AgentSession.fork_depth()`:** counts the depth of the fork chain.
- **`gc_sessions()`:** garbage-collects expired sessions. Materializes children before deleting parents. Walks oldest-first.
- **Compaction fork safety:** children are materialized before parent compaction, preventing broken references.
- **ACP `fork_session`:** uses real fork machinery instead of creating blank sessions. Accepts `fork_turn_id` kwarg, defaults to forking at last turn.
- **`aloop sessions` CLI subcommand:** `list`, `info <id>`, `gc [--max-age]`, `materialize <id>`.
- **[Sessions & Forking docs](docs/SESSIONS.md):** full reference for turn IDs, forking, materialization, GC, and design rationale.
- 58 new tests (447 total).

### Changed
- `list_sessions()` now returns `fork_from` and `fork_turn_id` in each session dict.
- Updated ARCHITECTURE.md, ACP.md, CLI.md, EMBEDDING.md, COMPACTION.md, README.md with forking references.

## [0.4.0] - 2026-04-03

### Added
- **Read-only exploration tools:** `grep` (wraps ripgrep), `find` (wraps fd, Python glob fallback), `ls` (pure Python readdir). Safe codebase exploration without shell access. Modeled on Pi's `readOnlyTools`.
- **Tool sets:** `CODING_TOOLS` (default — read, write, edit, bash, skill), `READONLY_TOOLS` (read, grep, find, ls, skill), `ALL_TOOLS` (everything). `ANALYSIS_TOOLS` kept as backward-compat alias for `CODING_TOOLS`.
- **Declarative permissions:** `permissions` config key in `.aloop/config.json` with path deny globs, project containment (`allow_outside_project`), additional dirs, write path restrictions. No config = no restrictions (yolo default).
- **Per-mode permissions:** modes can define `permissions` alongside `tools` for scoped restrictions.
- **`PermissionDenied`:** subclass of `ToolRejected` for permission check failures. Agents can distinguish "not allowed" from other hook rejections.
- **`"tools": ["*"]`** wildcard in mode config — selects all available tools including grep/find/ls.
- **Hardcoded safety net:** non-overridable write denies (`.git/**`, `.aloop/config.json`) and bash denies (`rm -rf /`, fork bombs, `mkfs`, `dd if=`). Always active.
- **Built-in permission hook** at priority 0 — runs before all user hooks. Enforces tool set, path restrictions, and hardcoded denies.
- **[Permissions docs](docs/PERMISSIONS.md):** full reference for security model, tool sets, config format, and design philosophy.
- 47 new tests (389 total).

### Changed
- Mode tool resolution now draws from `ALL_TOOLS` pool (was `ANALYSIS_TOOLS`), so modes can select grep/find/ls.
- Updated CONFIG.md, HOOKS.md, ARCHITECTURE.md, EMBEDDING.md, README.md with permissions references.

## [0.3.0] - 2026-04-03

### Breaking
- Renamed `AgentLoopBackend` to `ALoop` (`AgentLoopBackend` kept as deprecated alias with warning)
- Renamed `InferenceResult` to `RunResult` (`InferenceResult` kept as deprecated alias)
- `stream()` uses explicit `session_id` kwarg instead of `session_key` (`session_key` still accepted for backward compat)
- `stream()` uses explicit `context: dict` kwarg instead of `**kwargs` for hook context (`**kwargs` still accepted for backward compat)
- CLI restructured as subcommands (`aloop run`, `aloop serve`, `aloop config show`, etc.) — bare prompts still work via implicit `run` injection

### Added
- **Core API redesign:** `ALoop` class with `stream()` returning `AsyncIterator[InferenceEvent]` and `run()` returning `RunResult`
- **`LoopConfig` dataclass:** consolidates `max_iterations`, `max_session_age`, `max_session_messages`, and `compaction` settings
- **Full event protocol:** `LOOP_START`, `TURN_START`, `TEXT_DELTA`, `THINKING_DELTA`, `TOOL_START`, `TOOL_DELTA`, `TOOL_END`, `TURN_END`, `COMPACTION`, `LOOP_END`, `ERROR` — each event carries `timestamp`, `session_id`, `turn_id`, `tool_call_id`
- **`RunResult` enriched:** includes `input_tokens`, `output_tokens`, `cost_usd`, `model`, `turns`
- **Named modes:** define mode configs in `.aloop/config.json` with per-mode model, tools, system prompt, compaction, and iteration limits. `--mode` flag on CLI, `mode=` kwarg on `stream()`, `set_session_mode` in ACP. `ModeConflictError` on session mode conflicts.
- **10 hooks:** `on_loop_start`, `on_loop_end`, `on_turn_start`, `on_turn_end`, `before_tool`, `after_tool`, `on_pre_compaction`, `on_post_compaction`, `gather_context`, `register_tools`. Hook base class with optional method overrides. Priority ordering.
- **`ToolRejected` exception:** purpose-built exception for `before_tool` hooks to cancel tool calls with a reason string passed to the model
- **`@tool` decorator:** produces `ToolDef` from type hints using `Annotated[type, ToolParam(...)]`. Sync functions auto-wrapped to async. `ToolDef.timeout` for per-tool timeout override.
- **`ToolParam` dataclass:** metadata for tool parameters used with Annotated type hints
- **Unified instruction discovery:** `ALOOP.md` > `AGENTS.md` > `.agents/AGENTS.md` > `CLAUDE.md` > `.claude/CLAUDE.md` (first match wins, same chain for template and section mode)
- **Skills discovery (merged):** `.aloop/skills/` ∪ `.agents/skills/` ∪ `.claude/skills/` ∪ `~/.aloop/skills/` (project overrides global on name collision)
- **Global + project layering:** config deep-merged, hooks both run (global first), skills union by name, disable mechanism via `disabled_hooks` and `disabled_skills`
- **JSONC config:** all config files support `//` and `#` line comments. `strip_json_comments()` utility in `aloop.utils`.
- **CLI subcommands:** `run`, `serve`, `config show`, `config validate`, `providers list`, `providers validate`, `update`, `register-acpx`, `init`, `version`, `system-prompt`
- **`aloop init`:** scaffolds `.aloop/` directory with JSONC config, hooks template, and skills directory
- **`aloop config validate`:** validates all config files for JSONC parsing errors
- **`aloop config show`:** displays resolved config including instruction file, hooks, skills, provider, model
- **System prompt architecture:** section-based with 6 overridable sections, template mode with `{{tools}}`, `{{skills}}`, `{{agents_md}}` variables
- **ACP modes:** `set_session_mode` for per-session mode switching in ACP protocol

### Changed
- `InferenceEvent` now carries `timestamp`, `session_id`, `turn_id`, and `tool_call_id` fields
- System prompt sections joined with `---` dividers in section mode
- Tool merge order: mode base tools → hook tools → `extra_tools=` extends → `tools=` replaces
- Compaction hooks (`on_pre_compaction`, `on_post_compaction`) bracket the compaction operation
- `aloop serve` replaces the old `--acp` flag

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
