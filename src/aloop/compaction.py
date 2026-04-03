"""Context compaction for long-running agent sessions."""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from .models import ModelConfig

logger = logging.getLogger(__name__)

from . import get_project_root

MAX_TOOL_RESULT_CHARS = 50_000  # persistence threshold
PREVIEW_SIZE_BYTES = 2_000  # head preview for persisted results
PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"

# Tools whose output should never be persisted to disk
# (read_file is circular — persisting a read so the model reads it back)
PERSIST_EXEMPT_TOOLS: set[str] = {"read_file"}


def _get_compaction_config_path():
    return Path.home() / ".aloop" / "compaction.json"

@dataclass
class CompactionSettings:
    enabled: bool = True
    reserve_tokens: int = 16_384
    keep_recent_tokens: int = 20_000
    max_tool_result_chars: int = MAX_TOOL_RESULT_CHARS
    compact_instructions: str | None = None  # appended to summarization prompt


def get_compaction_settings() -> CompactionSettings:
    """Build CompactionSettings.

    Layers: global defaults → JSON config overrides (~/.aloop/compaction.json).
    """
    overrides: dict = {}

    # JSON config file overrides (user-editable)
    if _get_compaction_config_path().exists():
        try:
            data = json.loads(_get_compaction_config_path().read_text(encoding="utf-8"))
            for key in ("reserve_tokens", "keep_recent_tokens",
                        "max_tool_result_chars", "compact_instructions"):
                if key in data:
                    overrides[key] = data[key]
        except (OSError, json.JSONDecodeError):
            pass

    # Only pass valid fields to avoid TypeError
    valid_fields = {f.name for f in CompactionSettings.__dataclass_fields__.values()}
    filtered = {k: v for k, v in overrides.items() if k in valid_fields}
    return CompactionSettings(**filtered)


@dataclass
class FileOperations:
    read: set[str] = field(default_factory=set)
    written: set[str] = field(default_factory=set)
    edited: set[str] = field(default_factory=set)


@dataclass
class CompactionEntry:
    summary: str
    timestamp: float
    tokens_before: int
    first_kept_index: int
    file_ops: dict
    previous_summary: str | None = None


SUMMARIZATION_SYSTEM_PROMPT = (
    "You are a context summarization assistant. Read the conversation and produce "
    "a structured summary following the exact format specified. "
    "Do NOT continue the conversation. Do NOT respond to questions in it. "
    "ONLY output the structured summary."
)

INITIAL_SUMMARY_PROMPT = """Create a structured context checkpoint. Use this EXACT format:

## Goal
[What is the agent trying to accomplish?]

## Progress
### Done
- [x] [Completed tasks/changes with file paths]

### In Progress
- [ ] [Current work]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [What should happen next]

## Critical Context
- [Data, file paths, or references needed to continue]

Keep each section concise. Preserve exact file paths and data values."""

UPDATE_SUMMARY_PROMPT = """The messages above are NEW since the last summary. Update the existing summary.

RULES:
- PRESERVE all existing information from <previous-summary>
- ADD new progress, decisions, and context from new messages
- UPDATE Progress: move items from In Progress to Done when completed
- UPDATE Next Steps based on what was accomplished
- If something is no longer relevant, remove it

Use the same format as the previous summary."""


def estimate_tokens_chars(text: str) -> int:
    return max(1, math.ceil(len(text) / 4)) if text else 0


def estimate_message_tokens(msg: dict) -> int:
    chars = 0

    content = msg.get("content", "")
    if isinstance(content, str):
        chars += len(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                chars += len(str(block.get("text", "")))

    for tc in msg.get("tool_calls", []):
        fn = tc.get("function", {})
        chars += len(str(fn.get("name", "")))
        chars += len(str(fn.get("arguments", "")))

    return math.ceil(chars / 4) if chars > 0 else 0


def estimate_context_tokens(
    messages: list[dict],
    last_usage: dict | None = None,
    last_usage_index: int | None = None,
) -> int:
    if (
        last_usage
        and last_usage_index is not None
        and 0 <= last_usage_index < len(messages)
    ):
        usage_tokens = int(last_usage.get("prompt_tokens", 0) or 0) + int(
            last_usage.get("completion_tokens", 0) or 0
        )
        trailing_tokens = sum(
            estimate_message_tokens(m) for m in messages[last_usage_index + 1 :]
        )
        return usage_tokens + trailing_tokens

    return sum(estimate_message_tokens(m) for m in messages)


def should_compact(
    context_tokens: int,
    context_window: int,
    settings: CompactionSettings,
) -> bool:
    if not settings.enabled:
        return False
    return context_tokens > max(0, context_window - settings.reserve_tokens)


def extract_file_ops(
    messages: list[dict],
    previous_ops: FileOperations | None = None,
) -> FileOperations:
    ops = FileOperations()

    if previous_ops:
        ops.read = set(previous_ops.read)
        ops.written = set(previous_ops.written)
        ops.edited = set(previous_ops.edited)

    for msg in messages:
        if msg.get("_synthetic"):
            continue
        if msg.get("role") != "assistant":
            continue

        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args_raw = fn.get("arguments", "{}")

            try:
                args = json.loads(args_raw)
            except (TypeError, json.JSONDecodeError):
                continue

            path = args.get("path") or args.get("file_path")
            if not path:
                continue

            if name == "read_file":
                ops.read.add(path)
            elif name == "write_file":
                ops.written.add(path)
            elif name == "edit_file":
                ops.edited.add(path)

    return ops


def format_file_ops(ops: FileOperations) -> str:
    modified = sorted(ops.written | ops.edited)
    read_only = sorted(ops.read - ops.written - ops.edited)

    sections: list[str] = []
    if read_only:
        sections.append("<read-files>\n" + "\n".join(read_only) + "\n</read-files>")
    if modified:
        sections.append("<modified-files>\n" + "\n".join(modified) + "\n</modified-files>")

    return "\n\n".join(sections)


def find_cut_point(messages: list[dict], keep_recent_tokens: int) -> int:
    accumulated = 0
    for i in range(len(messages) - 1, -1, -1):
        accumulated += estimate_message_tokens(messages[i])
        if accumulated >= keep_recent_tokens:
            while i < len(messages) and messages[i].get("role") == "tool":
                i += 1
            return i
    return 0


async def compact_context(
    messages: list[dict],
    model_config: ModelConfig,
    settings: CompactionSettings,
    call_model_fn,
    previous_entry: CompactionEntry | None = None,
) -> tuple[list[dict], CompactionEntry | None]:
    tokens_before = estimate_context_tokens(messages)
    cut_index = find_cut_point(messages, settings.keep_recent_tokens)

    if cut_index <= 0:
        return messages, None

    old_messages = messages[:cut_index]
    kept_messages = messages[cut_index:]

    prev_ops = None
    if previous_entry and previous_entry.file_ops:
        prev_ops = FileOperations(
            read=set(previous_entry.file_ops.get("read", [])),
            written=set(previous_entry.file_ops.get("written", [])),
            edited=set(previous_entry.file_ops.get("edited", [])),
        )

    file_ops = extract_file_ops(old_messages, prev_ops)
    file_ops_text = format_file_ops(file_ops)

    conversation_text = _serialize_for_summary(old_messages)

    if previous_entry and previous_entry.summary:
        prompt = (
            f"<conversation>\n{conversation_text}\n</conversation>\n\n"
            f"<previous-summary>\n{previous_entry.summary}\n</previous-summary>\n\n"
            f"{UPDATE_SUMMARY_PROMPT}"
        )
    else:
        prompt = (
            f"<conversation>\n{conversation_text}\n</conversation>\n\n"
            f"{INITIAL_SUMMARY_PROMPT}"
        )

    if settings.compact_instructions:
        prompt += (
            f"\n\nAdditional summarization priorities:\n{settings.compact_instructions}"
        )

    summary = await call_model_fn(
        [{"role": "user", "content": prompt}],
        SUMMARIZATION_SYSTEM_PROMPT,
        model_config,
    )

    if file_ops_text:
        summary = f"{summary}\n\n{file_ops_text}"

    summary_message = {
        "role": "user",
        "content": (
            "The conversation history before this point was compacted "
            "into the following summary:\n\n"
            f"<summary>\n{summary}\n</summary>"
        ),
    }

    entry = CompactionEntry(
        summary=summary,
        timestamp=time.time(),
        tokens_before=tokens_before,
        first_kept_index=cut_index,
        file_ops={
            "read": sorted(file_ops.read),
            "written": sorted(file_ops.written),
            "edited": sorted(file_ops.edited),
        },
        previous_summary=previous_entry.summary if previous_entry else None,
    )

    return [summary_message] + kept_messages, entry


def _generate_preview(content: str, max_bytes: int = PREVIEW_SIZE_BYTES) -> str:
    """Head-only preview, cut at newline boundary when possible."""
    if len(content) <= max_bytes:
        return content

    truncated = content[:max_bytes]
    last_nl = truncated.rfind("\n")
    cut = last_nl if last_nl > max_bytes * 0.5 else max_bytes
    return content[:cut]


def persist_tool_result(
    content: str,
    tool_name: str,
    tool_call_id: str,
    overflow_dir: Path | None = None,
    max_chars: int = MAX_TOOL_RESULT_CHARS,
) -> str:
    """Persist large tool results to disk, return a preview.

    Results exceeding *max_chars* are saved to *overflow_dir* and the
    model receives a short head preview plus the file path so it can
    read the full output back.

    Tools in PERSIST_EXEMPT_TOOLS (e.g. read_file) are never persisted
    to avoid a circular read-back loop — they're returned as-is.
    """
    if len(content) <= max_chars:
        return content

    if tool_name in PERSIST_EXEMPT_TOOLS:
        return content

    if not overflow_dir:
        # No session dir available — fall back to head-only preview
        preview = _generate_preview(content)
        return (
            f"{PERSISTED_OUTPUT_TAG}\n"
            f"Output too large ({len(content):,} chars). No session directory for persistence.\n\n"
            f"Preview (first ~{PREVIEW_SIZE_BYTES} bytes):\n"
            f"{preview}\n...\n"
            f"{PERSISTED_OUTPUT_CLOSING_TAG}"
        )

    overflow_dir.mkdir(parents=True, exist_ok=True)
    ext = ".json" if content.lstrip().startswith(("{", "[")) else ".txt"
    filepath = overflow_dir / f"{tool_call_id}{ext}"
    filepath.write_text(content, encoding="utf-8")

    preview = _generate_preview(content)
    has_more = len(content) > PREVIEW_SIZE_BYTES

    return (
        f"{PERSISTED_OUTPUT_TAG}\n"
        f"Output too large ({len(content):,} chars). Full output saved to: {filepath}\n\n"
        f"Preview (first ~{PREVIEW_SIZE_BYTES} bytes):\n"
        f"{preview}"
        + ("\n...\n" if has_more else "\n")
        + PERSISTED_OUTPUT_CLOSING_TAG
    )


POST_COMPACT_MAX_FILES = 5
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000  # ~20K chars
POST_COMPACT_TOKEN_BUDGET = 50_000  # total across all restored files


def _collect_file_paths_in_messages(messages: list[dict]) -> set[str]:
    """Extract file paths from read_file tool calls in kept messages."""
    paths: set[str] = set()
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            if fn.get("name") == "read_file":
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except (TypeError, json.JSONDecodeError):
                    continue
                p = args.get("path") or args.get("file_path")
                if p:
                    paths.add(p)
    return paths


def restore_recent_files(
    file_ops: dict,
    kept_messages: list[dict] | None = None,
    max_files: int = POST_COMPACT_MAX_FILES,
    max_chars_per_file: int = POST_COMPACT_MAX_TOKENS_PER_FILE * 4,
    total_budget_chars: int = POST_COMPACT_TOKEN_BUDGET * 4,
) -> list[dict]:
    """Re-read recently accessed files after compaction.

    Sorted by most-recently accessed (mtime).  Deduplicates against
    files already in *kept_messages*.  Enforces per-file and total
    token budgets.
    """
    # Deduplicate: skip files already visible in the kept tail
    already_visible: set[str] = set()
    if kept_messages:
        already_visible = _collect_file_paths_in_messages(kept_messages)

    all_paths = set(file_ops.get("edited", [])) | set(file_ops.get("written", [])) | set(file_ops.get("read", []))
    candidates: list[tuple[float, str]] = []
    for path_str in all_paths:
        if path_str in already_visible:
            continue
        path = Path(path_str)
        if not path.is_absolute():
            path = get_project_root() / path
        try:
            if path.exists():
                candidates.append((path.stat().st_mtime, path_str))
        except Exception:
            continue

    # Sort by mtime descending (most recently accessed first)
    candidates.sort(key=lambda x: x[0], reverse=True)
    candidates = candidates[:max_files]

    if not candidates:
        return []

    file_blocks: list[str] = []
    used_chars = 0
    for _, path_str in candidates:
        path = Path(path_str)
        if not path.is_absolute():
            path = get_project_root() / path
        try:
            content = path.read_text(encoding="utf-8")
            if len(content) > max_chars_per_file:
                content = content[:max_chars_per_file] + "\n[... truncated]"
            block = f"<file path=\"{path_str}\">\n{content}\n</file>"
            if used_chars + len(block) > total_budget_chars:
                break
            file_blocks.append(block)
            used_chars += len(block)
        except Exception:
            continue

    if not file_blocks:
        return []

    return [
        {
            "role": "user",
            "content": (
                "Recently accessed files restored after context compaction:\n\n"
                + "\n\n".join(file_blocks)
            ),
        }
    ]


def _serialize_for_summary(messages: list[dict]) -> str:
    parts: list[str] = []

    for msg in messages:
        if msg.get("_synthetic"):
            continue
        role = msg.get("role", "unknown")
        content = msg.get("content", "")

        if isinstance(content, list):
            content = " ".join(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )

        if role == "user":
            parts.append(f"[User]: {content}")
            continue

        if role == "assistant":
            if content:
                parts.append(f"[Assistant]: {content}")

            tool_parts = []
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                args = str(fn.get("arguments", ""))[:200]
                tool_parts.append(f"{fn.get('name', '?')}({args})")

            if tool_parts:
                parts.append(f"[Assistant tool calls]: {'; '.join(tool_parts)}")
            continue

        if role == "tool":
            preview = str(content)[:500] if content else "(empty)"
            parts.append(f"[Tool result]: {preview}")

    return "\n\n".join(parts)
