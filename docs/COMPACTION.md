# Compaction

When a session's conversation history approaches the model's context window, aloop automatically compacts old messages into a structured summary. This lets long-running sessions continue without crashing or silently dropping context.

## How it works

```
1. Estimate token count of conversation history
2. If tokens > (context_window - reserve_tokens):
   ├── Run on_pre_compaction hooks (can inject preservation instructions)
   ├── Find cut point: walk backward, keep recent messages worth keep_recent_tokens
   ├── Extract file operations from old messages (which files were read/written/edited)
   ├── Serialize old messages for summarization
   ├── Send to model with structured summary prompt
   ├── Replace old messages with summary + kept recent messages
   ├── Restore recently-accessed files (re-read up to 5 files)
   ├── Emit COMPACTION event
   └── Run on_post_compaction hooks
3. Continue with compacted history
```

## When it triggers

Compaction triggers automatically when:

```
estimated_tokens > context_window - reserve_tokens
```

Default `reserve_tokens` is 16,384 — this leaves room for the model's response and tool calls. The check runs after every turn in the agent loop.

## What gets preserved

### The summary

Old messages are serialized into a structured checkpoint with this format:

```
## Goal
[What the agent was trying to accomplish]

## Progress
### Done
- [x] Completed tasks with file paths

### In Progress
- [ ] Current work

## Key Decisions
- **Decision**: Brief rationale

## Next Steps
1. What should happen next

## Critical Context
- Data, file paths, references needed to continue
```

If there was a previous compaction, the new summary updates the existing one — preserving accumulated context while adding new progress.

### File operations

The compaction system tracks which files were read, written, and edited throughout the session. These are included in the summary as XML tags:

```xml
<read-files>
src/main.py
tests/test_main.py
</read-files>

<modified-files>
src/auth.py
</modified-files>
```

### Recently-accessed files

After compaction, up to 5 recently-modified files are re-read and injected as messages. This restores working context that was lost in summarization.

| Constraint | Value |
|-----------|-------|
| Max files restored | 5 |
| Max tokens per file | 5,000 (~20K chars) |
| Total token budget | 50,000 (~200K chars) |

Files are sorted by mtime (most recently modified first). Files already visible in the kept recent messages are skipped (no duplicates).

## Large tool result persistence

Tool results exceeding 50,000 characters are persisted to disk instead of staying in the conversation history. The model receives a 2K preview plus a file path it can read back if needed.

```
<persisted-output>
Output too large (127,432 chars). Full output saved to:
  ~/.aloop/sessions/abc123_tool_results/call_xyz.txt

Preview (first ~2000 bytes):
[first 2K of output...]
...
</persisted-output>
```

`read_file` results are exempt — persisting a file read so the model reads it back would be circular.

## Circuit breaker

If compaction fails 3 consecutive times (model errors, timeout, malformed summary), auto-compaction is disabled for the rest of the session. This prevents wasting API calls on irrecoverable context. The counter resets on success.

## Configuration

### Via LoopConfig (Python API)

```python
from aloop import ALoop, LoopConfig
from aloop.compaction import CompactionSettings

loop = ALoop(
    model="x-ai/grok-4.1-fast",
    config=LoopConfig(
        compaction=CompactionSettings(
            reserve_tokens=16_384,      # buffer for model response
            keep_recent_tokens=20_000,  # how much recent context to preserve
            compact_instructions="Preserve all error messages and stack traces.",
        ),
    ),
)
```

### Via ~/.aloop/compaction.json

```jsonc
{
    // Buffer for model response (tokens)
    "reserve_tokens": 16384,

    // How much recent context to keep uncompacted (tokens)
    "keep_recent_tokens": 20000,

    // Extra instructions appended to the summarization prompt
    "compact_instructions": "Preserve all error messages and stack traces."
}
```

### Via mode config

Modes can specify per-mode compaction settings:

```jsonc
{
    "modes": {
        "deep-analysis": {
            "compaction": {
                "keep_recent_tokens": 40000,
                "compact_instructions": "Preserve all data tables and numerical results."
            }
        }
    }
}
```

## Hooks

Two hooks fire around compaction:

**`on_pre_compaction(context)`** — runs before compaction. Can return extra preservation instructions that are appended to the summarization prompt. Use this to dynamically specify what's important based on the current task.

**`on_post_compaction(context)`** — runs after successful compaction. Use for logging, updating external state, or metrics.

## Events

The `COMPACTION` event is emitted after every successful compaction:

```python
Event(
    type=EventType.COMPACTION,
    data={
        "messages_before": 47,
        "messages_after": 12,
        "tokens_saved": 35000,
    },
)
```

In NDJSON streaming output (`-o stream-json`):

```json
{"type": "compaction", "messages_before": 47, "messages_after": 12, "tokens_saved": 35000}
```

## How the summary prompt works

The model used for summarization is the same model running the session. It receives:

1. A system prompt: "You are a context summarization assistant..."
2. The serialized old messages wrapped in `<conversation>` tags
3. The previous summary (if any) wrapped in `<previous-summary>` tags
4. The structured format instructions
5. Any additional `compact_instructions` from config or hooks

The model is explicitly told to NOT continue the conversation — only produce the summary. The summary replaces all old messages as a single user message wrapped in `<summary>` tags.

## Debugging

Run `aloop config show` to see current compaction settings. If a session seems to lose context, check:

1. Are `keep_recent_tokens` and `reserve_tokens` appropriate for your model's context window?
2. Is the summarization model good enough? Smaller models may produce poor summaries.
3. Check `compact_instructions` — are you telling it to preserve what matters?
4. The `COMPACTION` event shows `messages_before`, `messages_after`, and `tokens_saved` — use this to understand how aggressively compaction is cutting.
