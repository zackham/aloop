# CLI Reference

aloop's CLI is a thin wrapper around the Python API. Use it for interactive work, scripting, and automation.

## Modes

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
cat error.log | aloop -p "What went wrong?"
echo "Summarize this" | aloop -p
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
aloop -c "Now explain the auth section"

# Resume a specific session by ID
aloop --resume a7b3c9d2e1f4 "What about the database config?"

# Use a memorable name instead of auto-generated ID
aloop -s refactor "Start refactoring the auth module"
aloop --resume refactor "Continue where we left off"
```

## Output formats

### text (default)

Streaming terminal output with ANSI formatting. Tool calls shown with box-drawing characters.

```bash
aloop -p "List all Python files"
```

### json

Final result as a single JSON object to stdout. Nothing else printed.

```bash
aloop -p -o json "What is 2+2?"
```

```json
{"text": "4", "session_id": "a7b3c9d2e1f4", "usage": {"input_tokens": 150, "output_tokens": 20, "cost_usd": 0.001}, "cost_usd": 0.001}
```

Good for: capturing results in scripts, piping to `jq`, integrating with other tools.

### stream-json

NDJSON (newline-delimited JSON) events as they happen.

```bash
aloop -p -o stream-json "List all files"
```

```json
{"type": "turn", "iteration": 0}
{"type": "text", "text": "Here"}
{"type": "text", "text": " are"}
{"type": "text", "text": " the files:"}
{"type": "tool_start", "name": "bash", "args": {"command": "ls"}}
{"type": "tool_end", "name": "bash", "result": "README.md\nsrc/\ntests/", "is_error": false}
{"type": "text", "text": "\n\nThe project contains..."}
{"type": "complete", "text": "...", "session_id": "abc123", "usage": {...}}
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
aloop list-providers                                        # show all providers
aloop validate-provider --provider openai --model gpt-4o    # test a provider
```

## Commands

These are positional subcommands (pass as the prompt argument):

| Command | Description |
|---------|-------------|
| `aloop update` | Self-update to latest version from GitHub |
| `aloop register-acpx` | Register aloop with acpx for ACP integration |
| `aloop list-providers` | Show available API providers with test status |
| `aloop validate-provider` | Test a provider's streaming, tool calling, and multi-turn |
| `aloop system-prompt` | Show the raw system prompt template |
| `aloop system-prompt --rendered` | Show the fully interpolated prompt the model receives |

## All flags

```
aloop [PROMPT] [OPTIONS]

Options:
  --model, -m MODEL          Model ID (or set ALOOP_MODEL env var)
  --provider PROVIDER        API provider name (default: openrouter)
  -p                         One-shot mode: print response and exit
  -c, --continue             Continue the most recent session
  --resume SESSION_ID        Resume a specific session by ID
  -s, --session KEY          Use a named session instead of auto-generated ID
  -o, --output-format FMT    text (default), json, or stream-json
  --tools NAMES              Comma-separated tool names (filters built-in tools)
  --no-context               Skip gather_context hook injection
  --max-iterations N         Max agent loop iterations (default: 50)
  --acp                      Run as ACP server over stdio
  --version                  Print version and exit
  --list-models              List registered model aliases from ~/.aloop/models.json
```

## Examples

```bash
# Quick question, one-shot
aloop -p "What does main.py do?"

# Interactive refactoring session with a name
aloop -s auth-refactor "Let's refactor the authentication module"

# Pipe a file and get JSON output
cat requirements.txt | aloop -p -o json "Are there any security vulnerabilities?"

# Stream events for a custom UI
aloop -p -o stream-json "Build a REST API for users" | while read line; do
    echo "$line" | jq -r 'select(.type == "text") | .text' 2>/dev/null
done

# Use a specific provider and model
aloop --provider anthropic --model claude-sonnet-4-20250514 "Explain this error"

# Continue where you left off
aloop -c "What was the next step?"

# Headless automation
ALOOP_MODEL=x-ai/grok-4.1-fast aloop -p -o json "Generate a migration script" > migration.json
```
