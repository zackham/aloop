# Changelog

All notable changes to aloop are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.7.0] - 2026-04-15

### Added
- **`ALoop.complete()`:** one-shot completion method for prompt → text without the agent loop. No tools, no session, no hooks, no compaction. Uses the same provider/model/api_key configuration as `stream()` and `run()`. Returns a `RunResult` with text, token counts, and cost.
- **`_stream_completion` overrides:** optional keyword-only `temperature` and `max_tokens` parameters. Used internally by `complete()` — existing callers unaffected.

## [0.6.0] - 2026-04-06

### Added
- **Subagent support:** spawn child agents via the new built-in `agent` tool. Two paths — fork (inherit parent context via session forking + a fork-boilerplate directive) and fresh (clean session with scoped mode config). Recursive forking is allowed; fork children inherit the parent's mode label so they can spawn further.
- **`agent` built-in tool:** model-facing delegation primitive. Auto-injected into modes that opt in via `spawnable_modes` or `can_fork`. The description is computed per-call from the mode's spawnable list. Returns the child's final assistant text plus a structured lineage footer.
- **`AgentExecutor` protocol:** abstraction boundary between the agent tool and the spawn mechanism. `InProcessExecutor` is the default and only v0.6.0 implementation. A future subprocess executor can be slotted in without touching the agent tool.
- **`AgentResult` dataclass:** structured result from spawned children — `text`, `session_id`, `spawn_kind`, `mode`, `parent_session_id`, `parent_turn_id`, `usage`.
- **`extract_partial_result()`:** walks child message history backwards to find the last assistant text block. Used as fallback when a child exits without LOOP_END (e.g. hit max_iterations).
- **Mode config fields:**
  - `subagent_eligible: bool` — must be true for a mode to appear in another mode's `spawnable_modes` list.
  - `spawnable_modes: list[str]` — per-mode allowlist of modes that can be spawned via the agent tool's fresh path.
  - `can_fork: bool` — controls whether the fork path (mode omitted) is available.
- **`spawn_metadata` on sessions:** child sessions persist `kind`, `parent_session_id`, `parent_turn_id`, `spawning_mode`, `child_mode`, `timestamp` for lineage tracking. Visible in `aloop sessions info <id>`.
- **`validate_subagent_config()`:** validation function called by `aloop config validate` to catch invalid `spawnable_modes` references and missing `subagent_eligible` flags.
- **`turn_id` and `session_id` injected into `_context`:** tools that declare a `_context` parameter now receive the current `turn_id` and `session_id` automatically. The agent tool relies on this to fork at the right point.
- **Parent session save before tool loop:** when an assistant turn produces tool calls, the parent's session is now persisted before tools run. Ensures fork-during-tool-call sees the latest turn on disk.
- ~110 new tests (570 total).

### Changed
- `aloop sessions info <id>` now displays spawn metadata when present.
- `__version__` bumped to 0.6.0 (note: 0.5.0 was inadvertently shipped as 0.4.0 in `__init__.py`; this release corrects that).
- `ALoop.__init__` accepts an optional `executor: AgentExecutor` parameter.

### Fixed
- **CLI `--mode` now respects mode's tool list.** The `aloop run` subcommand was always passing `tools=ANALYSIS_TOOLS` explicitly to `stream()`, which the agent loop treats as a full override of any mode-defined tool list. Result: `aloop run --mode foo` silently ignored `foo`'s `tools: [...]` config and gave the model the default CODING_TOOLS set, breaking the structural escalation prevention model for subagents. Bug existed since v0.4.0 (when mode-tools support landed) but only became visible after v0.6.0 added the auto-injected `agent` tool and structural permission enforcement. Fix: when `--mode` is set without an explicit `--tools`, leave `tools=` unset so the mode's tool list takes effect inside `stream()`. Explicit `--tools` still wins as an override.

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
