# ACP Integration

aloop has a built-in [Agent Client Protocol](https://agentclientprotocol.com) (ACP) server. This lets it work with any ACP client: editors (Zed, JetBrains, Neovim), headless clients ([acpx](https://github.com/openclaw/acpx)), and orchestrators ([Stepwise](https://github.com/zackham/stepwise)).

## Setup

```bash
aloop register-acpx
```

This writes `{"agents": {"aloop": {"command": "aloop serve"}}}` to `~/.acpx/config.json`. Run once. Idempotent.

Requires [acpx](https://github.com/openclaw/acpx) installed: `npm install -g acpx`

## Using with acpx

```bash
# Basic prompt
acpx aloop "Explain the auth module"

# With a specific model
acpx --model x-ai/grok-4.1-fast aloop "Refactor this function"

# Named session for multi-turn
acpx aloop sessions new --name backend
acpx aloop --session backend "Read the API routes"
acpx aloop --session backend "Now add error handling to the /users endpoint"

# One-shot (no saved session)
acpx aloop exec "What does this repo do?"

# JSON output for scripting
acpx --format json aloop "List all TODO comments"
```

### Environment

Set `ALOOP_MODEL` in your shell — acpx passes the parent environment to the agent subprocess:

```bash
export ALOOP_MODEL="x-ai/grok-4.1-fast"
acpx aloop "prompt"
```

## Using with Stepwise

aloop works as a step executor in [Stepwise](https://github.com/zackham/stepwise) flows:

```yaml
name: code-review
steps:
  review:
    executor: agent
    agent: aloop
    working_dir: ~/work/my-project
    prompt: |
      Review the changes in the last commit. Focus on:
      - Security issues
      - Performance regressions
      - Missing error handling
      Report findings as a markdown checklist.
    output_mode: stream_result
    permissions: approve_all
    timeout: 300
    outputs: [result]

  fix:
    executor: agent
    agent: aloop
    working_dir: ~/work/my-project
    prompt: |
      Fix the issues identified in the review:
      $result
    permissions: approve_all
    timeout: 600
    inputs:
      result: review.result
```

```bash
ALOOP_MODEL=x-ai/grok-4.1-fast stepwise run flow.yaml --wait
```

### Output modes

| Stepwise `output_mode` | What it captures |
|------------------------|-----------------|
| `effect` (default) | Agent's side effects (file changes, etc.) |
| `stream_result` | Final text from agent's last message |
| `file` | JSON written to `$STEPWISE_OUTPUT_FILE` |

### Model selection

Stepwise passes the parent environment to agent subprocesses, so set `ALOOP_MODEL` where Stepwise runs. Or bake the model into the acpx agent config:

Edit `~/.acpx/config.json`:

```json
{
  "agents": {
    "aloop": {"command": "aloop serve --model x-ai/grok-4.1-fast"}
  }
}
```

Then `agent: aloop` in any flow uses that model without env vars.

## Using with editors

Any editor that supports ACP can drive aloop. The agent appears as an available coding agent in the editor's agent panel.

### Zed

After `aloop register-acpx`, aloop appears in Zed's agent picker. Select it and chat normally. Zed handles session management, permission prompts, and file operations through ACP.

### JetBrains / Neovim

Editors with ACP support via plugins work the same way — they spawn `aloop serve` as a subprocess and communicate over stdio using the ACP JSON-RPC protocol.

## How it works

`aloop serve` runs as an ACP server over stdio (JSON-RPC 2.0, NDJSON):

```
Client (acpx/editor)              aloop serve
    │                                  │
    │── initialize ──────────────────► │
    │◄── protocol_version, caps ────── │
    │                                  │
    │── session/new (cwd) ───────────► │  creates ALoop
    │◄── session_id ───────────────── │
    │                                  │
    │── session/prompt ──────────────► │  drives backend.stream()
    │◄── session_update (text) ────── │  ← TEXT_DELTA
    │◄── session_update (tool_call) ─ │  ← TOOL_START
    │◄── session_update (tool_done) ─ │  ← TOOL_END
    │◄── session_update (usage) ───── │  ← TURN_END
    │◄── prompt_response ──────────── │
    │                                  │
    │── session/cancel ──────────────► │  sets cancel event
    │── session/close ───────────────► │  cleans up
```

### Event mapping

| aloop InferenceEvent | ACP SessionUpdate |
|---------------------|-------------------|
| `TEXT_DELTA` | `agent_message_chunk` |
| `THINKING_DELTA` | `agent_thought_chunk` |
| `TOOL_START` | `tool_call` (status: in_progress) |
| `TOOL_DELTA` | `tool_call_update` (partial content) |
| `TOOL_END` | `tool_call_update` (status: completed/failed) |
| `TURN_END` | `usage_update` (per-turn cost, tokens) |
| `COMPACTION` | logged (no ACP notification) |
| `LOOP_END` | end_turn (stop reason) |

### ACP methods supported

| Method | Description |
|--------|-------------|
| `initialize` | Handshake — returns protocol version, capabilities |
| `session/new` | Create session with working directory |
| `session/load` | Restore or create session by ID |
| `session/prompt` | Send prompt, stream response events |
| `session/cancel` | Cancel in-flight prompt |
| `session/close` | Clean up session state |
| `session/fork` | Branch a session |
| `session/list` | List known sessions |
| `set_session_mode` | Switch to a named mode config (model, tools, system prompt, compaction) |
| `set_session_model` | Change model for a session (used by `acpx --model`) |

### Per-session state

Each ACP session gets its own `ALoop` instance with independent:
- Token counters and cost tracking
- Compaction state and circuit breaker
- Conversation history
- Cancel event

Sessions persist to `~/.aloop/sessions/` and survive agent restarts (acpx reconnects via `session/load`).
