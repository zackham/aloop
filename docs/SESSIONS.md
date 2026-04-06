# Sessions & Forking

aloop sessions persist conversation history to disk. Forking lets you branch a session at any turn — for subagent patterns, edit+rerun workflows, or chat UIs that support branching.

## Session Storage

Each session is stored as two files in `~/.aloop/sessions/`:

| File | Purpose | Access pattern |
|------|---------|---------------|
| `{id}.context.json` | Current state — messages, compaction, fork metadata | Overwritten on each save |
| `{id}.log.jsonl` | Full history — every message, append-only | Append-only, never truncated |

Context is the hot path (fast load on resume). Log is cold (debugging, analytics, replay).

## Turn IDs

Every message carries a `turn_id` — a 12-character hex string generated per turn. A **turn** is everything from one user prompt through the final assistant response, including any tool call cycles. All messages in a turn share the same `turn_id`.

```json
{"role": "user", "content": "fix the tests", "turn_id": "a1b2c3d4e5f6"}
{"role": "assistant", "content": null, "turn_id": "a1b2c3d4e5f6", "tool_calls": [...]}
{"role": "tool", "content": "...", "turn_id": "a1b2c3d4e5f6"}
{"role": "assistant", "content": "Found the issue.", "turn_id": "a1b2c3d4e5f6"}
```

Turn IDs are persisted in both `context.json` and `log.jsonl`. They're the addressing unit for forking — you fork at turn boundaries, never mid-tool-chain.

## Forking

### How it works

A fork creates a new session that references a parent session at a specific turn. The child starts with empty messages — on load, it walks the parent chain to reconstruct the full history.

```
Parent session:  [t1] [t2] [t3] [t4]
                         ↑
Fork at t2:      [t1] [t2] ← from parent
Child messages:              [t5] [t6] ← child's own
```

On disk, the child's `context.json` stores only its own messages plus a pointer:

```json
{
  "session_id": "child_abc",
  "fork_from": "parent_xyz",
  "fork_turn_id": "a1b2c3d4e5f6",
  "messages": [...]
}
```

No message duplication — parent history is reconstructed on load.

### Recursive chain walk

Forks can be nested. A child can be forked to create a grandchild. Loading walks the chain recursively:

```python
# Grandchild resolves as:
# grandparent messages up to fork point
# + parent messages up to fork point
# + grandchild's own messages
resolved = grandchild.resolve_messages()
```

An auto-materialize safety valve triggers at depth 10 to bound chain walks. In practice, subagent depth rarely exceeds 3-4.

### Immutability

Parent messages up to the fork point are treated as immutable by the child. The parent can continue adding messages after the fork — the child only sees messages up to its `fork_turn_id`. Both branches evolve independently.

### Python API

```python
from aloop import ALoop, EventType

backend = ALoop(model="x-ai/grok-4.1-fast", api_key="...")

# Fork from a specific turn
async for event in backend.stream(
    "try a different approach",
    fork_from="parent_session_id",
    fork_at="turn_005",
):
    ...

# Fork from the latest turn (fork_at defaults to last turn)
async for event in backend.stream(
    "explore this alternative",
    fork_from="parent_session_id",
):
    ...

# Edit+rerun: truncate at a turn and replace it
async for event in backend.stream(
    "better prompt for this step",
    session_id="existing_session",
    replace_turn="turn_003",
):
    ...
```

**`fork_from`** — parent session_id. Creates a new child session.

**`fork_at`** — turn_id in the parent to fork at (inclusive). If omitted, forks at the last turn.

**`replace_turn`** — turn_id to replace. Truncates the session's messages to just before that turn, then continues with the new prompt. This mutates the existing session (not a fork). Use `fork_from` + `fork_at` for non-destructive edit+rerun.

### Session inspection

```python
from aloop.session import AgentSession

session = AgentSession.load("session_id")

# Fork metadata
session.fork_from       # parent session_id, or None
session.fork_turn_id    # turn forked at, or None
session.fork_depth()    # chain depth (0 for non-forked)
session.children()      # list of session_ids that fork from this one

# Full message history (walks parent chain)
messages = session.resolve_messages()
```

### Materialization

Materialization flattens a fork chain into a standalone session — resolving all parent messages into the session's own message list and severing the parent dependency.

```python
session.materialize()
# session.fork_from is now None
# session.messages contains the full history
# parent can be safely deleted
```

**When to materialize:**
- Before deleting a parent session
- Before exporting or sharing a session
- When a branch becomes the "main" conversation
- When preserving a subagent session long-term

Materialization is explicit. The library does not auto-materialize except at the depth-10 safety valve.

## Compaction & Forks

Before compacting a session, aloop materializes all its children. This ensures no child is left referencing messages that were rewritten by compaction.

The sequence:
1. Session hits compaction threshold
2. Find all children (sessions with `fork_from` pointing here)
3. Materialize each child (flatten parent messages into the child)
4. Compact the parent freely

After materialization, children are standalone — the parent's compaction doesn't affect them.

## Garbage Collection

Sessions expire based on age. GC materializes children before deleting expired parents.

### CLI

```bash
# Delete sessions older than 7 days (default)
aloop sessions gc

# Custom max age (in seconds)
aloop sessions gc --max-age 86400
```

### Python API

```python
from aloop.session import gc_sessions

deleted = gc_sessions(max_age_seconds=604800)  # 7 days
print(f"Deleted {len(deleted)} sessions")
```

### GC behavior

1. Load all sessions, sort by `last_active` ascending (oldest first)
2. For each expired session:
   - Find children (sessions with `fork_from` pointing here)
   - Materialize each child
   - Delete the parent's `.context.json` and `.log.jsonl`
3. Return list of deleted session_ids

GC is explicit — run it via `aloop sessions gc` or call `gc_sessions()`. There is no automatic background cleanup.

## CLI Reference

```bash
aloop sessions list                        # list all sessions with fork metadata
aloop sessions info <session_id>           # show session details
aloop sessions gc [--max-age SECONDS]      # garbage-collect expired sessions
aloop sessions materialize <session_id>    # flatten a forked session
```

### `aloop sessions list`

Shows all sessions sorted by most recently active. Fork relationships are indicated:

```
  a7b3c9d2e1f4  12 messages
  f8e2d1c0b9a8   4 messages  (fork of a7b3c9d2e1f4)
  c3d4e5f6a7b8   1 messages
3 session(s)
```

### `aloop sessions info <id>`

```
  Session:     a7b3c9d2e1f4
  Messages:    12
  Fork from:   (none)
  Fork turn:   (none)
  Fork depth:  0
  Children:    f8e2d1c0b9a8
```

## Fork Index

aloop maintains a lightweight index at `~/.aloop/sessions/_fork_index.json` that maps parent session IDs to their children. This makes `children()` lookups O(1) instead of scanning every session file.

The index is a **cache, not source of truth**. It is:
- Updated incrementally on fork creation, materialization, and GC
- Rebuilt automatically if missing or corrupt (falls back to O(n) scan)
- Written atomically (tmp + rename)
- Silently skipped on write failure (disk full, permissions)

```bash
# Force a rebuild (if you suspect stale data)
aloop sessions rebuild-index
```

## Design Notes

### Why parent pointers (not copy-on-fork)

Copy-on-fork duplicates all parent messages into the child. Parent pointers store only a reference — child sessions are lightweight. Multiple forks from the same parent share the parent's messages on disk with zero duplication.

Trade-off: loading a forked session requires reading the parent too. With recursive chains, this means reading N files for depth N. The depth-10 auto-materialize bounds this.

### Why turn boundaries only

Forking mid-tool-chain (after the assistant requested a tool call but before processing the result) would leave the child in an inconsistent state. Turn boundaries are the only semantically clean fork points.

### Why no merge

Conversation branches aren't code — there's no meaningful automatic merge of divergent conversation paths. If you need to combine insights from two branches, fork a new session and reference what you want. The fork graph is always a tree, never a DAG.
