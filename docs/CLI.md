# CLI Reference

aloop's CLI is a thin wrapper around the Python API. Use it for interactive work, scripting, and automation.

## Subcommands

```
aloop run [PROMPT]              Run a prompt (default when bare prompt given)
aloop serve                     Run as ACP server over stdio
aloop sessions list             List all sessions with fork metadata
aloop sessions info ID          Show session details (messages, forks, children)
aloop sessions gc [--max-age S] Garbage-collect expired sessions (default: 7 days)
aloop sessions materialize ID   Flatten a forked session into standalone
aloop sessions rebuild-index    Rebuild fork index from session files
aloop config show               Show resolved configuration
aloop config validate           Validate config files (JSONC parsing)
aloop providers list            List available API providers
aloop providers validate        Test a provider's API compatibility
aloop update                    Self-update to latest version
aloop register-acpx             Register aloop with acpx for ACP integration
aloop init                      Scaffold .aloop/ directory
aloop version                   Print version and exit
aloop system-prompt             Show system prompt template
```

The `run` subcommand is the default — bare prompts are treated as `aloop run "prompt"`.

## Running prompts

### Interactive (default)

```bash
aloop "Read README.md and explain it"
```

Runs the prompt, then drops into a REPL for follow-up questions. A session is auto-created. The session ID is printed when you exit (ctrl-c, ctrl-d, or type `exit`).

### One-shot (`-p`)

```bash
aloop -p "What is 2+2?"
```

Prints the response and exits. The session ID is printed to stderr so it doesn't pollute stdout. Use this for scripting.

### Pipe

```bash
cat error.log | aloop "What went wrong?"
echo "Summarize this" | aloop
```

Pipe input implies `-p` (one-shot). Stdin is used as the prompt.

## Sessions

Every invocation creates a session automatically. Sessions persist conversation history to `~/.aloop/sessions/`.

```bash
# Auto-generated session ID
aloop "Read the config files"
# ... REPL ...
# (exit)
# session: a7b3c9d2e1f4

# Continue the most recent session
aloop --continue "Now explain the auth section"

# Resume a specific session by ID
aloop --resume a7b3c9d2e1f4 "What about the database config?"

# Use a memorable name instead of auto-generated ID
aloop --session refactor "Start refactoring the auth module"
aloop --resume refactor "Continue where we left off"
```

## Session management

```bash
# List all sessions
aloop sessions list

# Show details for a session (messages, fork info, children)
aloop sessions info a7b3c9d2e1f4

# Garbage-collect sessions older than 7 days
aloop sessions gc

# Custom max age (in seconds)
aloop sessions gc --max-age 86400

# Materialize a forked session (flatten parent chain, sever dependency)
aloop sessions materialize f8e2d1c0b9a8

# Rebuild fork index (if you suspect stale data)
aloop sessions rebuild-index
```

See [Sessions & Forking](SESSIONS.md) for details on forking, materialization, and garbage collection.

## Output formats

### text (default)

Streaming terminal output with ANSI formatting. Tool calls shown with box-drawing characters.

```bash
aloop -p "List all Python files"
```

### json

Final result as a single JSON object to stdout. Nothing else printed.

```bash
aloop -p --output-format json "What is 2+2?"
```

```json
{"text": "4", "session_id": "a7b3c9d2e1f4", "usage": {"input_tokens": 150, "output_tokens": 20, "cost_usd": 0.001}, "cost_usd": 0.001}
```

Good for: capturing results in scripts, piping to `jq`, integrating with other tools.

### stream-json

NDJSON (newline-delimited JSON) events as they happen.

```bash
aloop -p --output-format stream-json "List all files"
```

```json
{"type": "turn", "iteration": 0}
{"type": "text", "text": "Here"}
{"type": "text", "text": " are"}
{"type": "text", "text": " the files:"}
{"type": "tool_start", "name": "bash", "args": {"command": "ls"}}
{"type": "tool_end", "name": "bash", "result": "README.md\nsrc/\ntests/", "is_error": false}
{"type": "text", "text": "\n\nThe project contains..."}
{"type": "turn_end", "iteration": 0, "turn_id": "abc123", "input_tokens": 150, "output_tokens": 80, "cost_usd": 0.001}
{"type": "loop_end", "text": "...", "session_id": "abc123", "input_tokens": 150, "output_tokens": 80, "cost_usd": 0.001, "model": "x-ai/grok-4.1-fast", "turns": 1}
```

Good for: real-time streaming to a UI, log aggregation, building custom frontends.

## Providers and models

```bash
# Default provider (openrouter)
aloop --model x-ai/grok-4.1-fast "prompt"

# Specify provider explicitly
aloop --provider openai --model gpt-4o "prompt"
aloop --provider google --model gemini-2.5-flash "prompt"
aloop --provider ollama --model llama3 "prompt"

# Set defaults via environment
export ALOOP_MODEL="x-ai/grok-4.1-fast"
export OPENROUTER_API_KEY="sk-or-..."
aloop "prompt"
```

### Provider management

```bash
aloop providers list                                          # show all providers
aloop providers validate --provider openai --model gpt-4o     # test a provider
```

## System prompt

```bash
aloop system-prompt                # show the raw system prompt template
aloop system-prompt --rendered     # show the fully interpolated prompt

# Override system prompt for a single run
aloop --system-prompt "You are a pirate." "Tell me about ships"
aloop --system-prompt-file /path/to/prompt.md "Hello"
```

## Configuration

```bash
aloop config show     # show resolved config: instructions, hooks, skills, provider, model
aloop init            # scaffold .aloop/ directory with config, hooks, and skills dirs
```

`aloop config show` displays the resolved state: which instruction file was found (and which were skipped), loaded hooks, loaded skills, active provider, model, and config file paths. Useful for debugging project setup.

`aloop init` creates a `.aloop/` directory in the current working directory with:
- `config.json` — JSONC config with commented-out examples explaining options (comments are stripped at parse time)
- `hooks/__init__.py` — hook decorator and example
- `skills/` — empty directory for project skills

`aloop config validate` checks all config files for JSONC syntax errors (global, project, compaction, models, providers, credentials).

## ACP server

```bash
aloop serve                                    # start ACP server on stdio
aloop serve --model gpt-4o --provider openai   # with specific model/provider
aloop register-acpx                            # register with acpx
```

## `aloop run` flags

```
aloop run [PROMPT] [OPTIONS]

Positional:
  PROMPT                         Prompt text (optional for interactive mode)

Options:
  --model, -m MODEL              Model ID (or set ALOOP_MODEL env var)
  --provider PROVIDER            API provider name (default: openrouter)
  -p                             One-shot mode: print response and exit
  -c, --continue                 Continue the most recent session
  --resume SESSION_ID            Resume a specific session by ID
  -s, --session KEY              Use a named session instead of auto-generated ID
  -o, --output-format FMT        text (default), json, or stream-json
  --system-prompt TEXT           Override system prompt text
  --system-prompt-file PATH      Override system prompt from a file
  --mode MODE                    Named mode from .aloop/config.json modes section
  --tools NAMES                  Comma-separated tool names (filters built-in tools)
  --no-context                   Skip gather_context hook injection
  --max-iterations N             Max agent loop iterations (default: 50)
  --thinking enabled|disabled    Reasoning toggle (DeepSeek V4 etc.)
  --reasoning-effort high|max    Reasoning effort level
```

## Reasoning / thinking flags

For thinking-capable models (DeepSeek V4 today), `--thinking` and `--reasoning-effort` map straight to the request payload. They work on both `aloop run` and `aloop complete`. Per-call flags override mode config and constructor defaults.

```bash
# Visible thinking stream, max effort
aloop --provider deepseek --model deepseek-v4-pro \
  --thinking enabled --reasoning-effort max "design a sharded queue with at-most-once semantics"

# Same model, fast / cheap path
aloop --provider deepseek --model deepseek-v4-flash --thinking disabled "what's 2+2?"

# Mode bakes the knobs in
aloop --mode deepseek-pro-max "..."
```

## Modes

Named mode configs switch model, tools, system prompt, and compaction per session. Define modes in `.aloop/config.json` (see [CONFIG.md](CONFIG.md#modes)).

```bash
# Use a review mode (read-only tools, reviewer system prompt)
aloop --mode review "Check this PR for bugs"

# Use a fast mode (different model, fewer iterations)
aloop --mode fast "Quick question about the API"

# Mode + explicit overrides (explicit flags win over mode config)
aloop --mode review --system-prompt "Be extra strict." "Review auth.py"
```

When `--mode` is set:
- The mode's `system_prompt` or `system_prompt_file` is used unless `--system-prompt` or `--system-prompt-file` is also specified.
- The mode's `tools` list filters available tools. The `--tools` flag overrides mode tools entirely.
- The mode's `model` and `provider` are used for the session.

## Examples

```bash
# Quick question, one-shot
aloop -p "What does main.py do?"

# Interactive refactoring session with a name
aloop --session auth-refactor "Let's refactor the authentication module"

# Pipe a file and get JSON output
cat requirements.txt | aloop --output-format json "Are there any security vulnerabilities?"

# Stream events for a custom UI
aloop -p --output-format stream-json "Build a REST API for users" | while read line; do
    echo "$line" | jq -r 'select(.type == "text") | .text' 2>/dev/null
done

# Use a specific provider and model
aloop --provider anthropic --model claude-sonnet-4-20250514 "Explain this error"

# Continue where you left off
aloop --continue "What was the next step?"

# Headless automation
ALOOP_MODEL=x-ai/grok-4.1-fast aloop -p --output-format json "Generate a migration script" > migration.json

# Debug project configuration
aloop config show

# Scaffold a new project
cd my-project && aloop init
```
