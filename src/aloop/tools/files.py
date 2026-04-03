"""File operation tools for background inference agents."""

from __future__ import annotations

from pathlib import Path

from ..tools_base import ToolDef, ToolResult
from .. import get_project_root


def _resolve_path(path_str: str) -> Path:
    path = Path(path_str)
    if not path.is_absolute():
        path = get_project_root() / path
    return path.resolve()


def _check_path(path_str: str, allow_write: bool = False) -> Path:
    """Resolve a path relative to project root.

    No access restrictions by default — the agent can read and write
    anywhere under the project root. Projects can add restrictions
    via before_tool hooks.
    """
    return _resolve_path(path_str)


read_file_tool = ToolDef(
    name="read_file",
    description="Read file contents with optional line offset and limit.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or repo-relative file path"},
            "offset": {"type": "integer", "description": "Start line (0-indexed)", "default": 0},
            "limit": {"type": "integer", "description": "Max lines to read", "default": 2000},
        },
        "required": ["path"],
    },
    execute=None,
)


async def _read_file(path: str, offset: int = 0, limit: int = 2000) -> ToolResult:
    try:
        resolved = _check_path(path, allow_write=False)
        if not resolved.exists():
            return ToolResult(content=f"File not found: {path}", is_error=True)

        lines = resolved.read_text(encoding="utf-8").splitlines()
        start = max(0, int(offset))
        max_lines = max(1, min(int(limit), 5000))
        selected = lines[start : start + max_lines]

        content = "\n".join(f"{i + start + 1}: {line}" for i, line in enumerate(selected))
        if len(lines) > start + max_lines:
            content += f"\n... ({len(lines) - start - max_lines} more lines)"

        return ToolResult(content=content)
    except PermissionError as exc:
        return ToolResult(content=str(exc), is_error=True)
    except Exception as exc:  # pragma: no cover - defensive
        return ToolResult(content=f"Error reading file: {exc}", is_error=True)


read_file_tool.execute = _read_file


write_file_tool = ToolDef(
    name="write_file",
    description="Create or overwrite a file.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or repo-relative file path"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["path", "content"],
    },
    execute=None,
)


async def _write_file(path: str, content: str) -> ToolResult:
    try:
        resolved = _check_path(path, allow_write=True)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return ToolResult(content=f"Written {len(content)} chars to {resolved}")
    except PermissionError as exc:
        return ToolResult(content=str(exc), is_error=True)
    except Exception as exc:  # pragma: no cover - defensive
        return ToolResult(content=f"Error writing file: {exc}", is_error=True)


write_file_tool.execute = _write_file


edit_file_tool = ToolDef(
    name="edit_file",
    description="Find and replace a unique string in a file.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or repo-relative file path"},
            "old_string": {"type": "string", "description": "Text to find"},
            "new_string": {"type": "string", "description": "Replacement text"},
        },
        "required": ["path", "old_string", "new_string"],
    },
    execute=None,
)


def _normalize_quotes(s: str) -> str:
    """Normalize curly/smart quotes to straight ASCII quotes."""
    return (
        s.replace("\u2018", "'")  # left single
        .replace("\u2019", "'")   # right single
        .replace("\u201c", '"')   # left double
        .replace("\u201d", '"')   # right double
    )


async def _edit_file(path: str, old_string: str, new_string: str) -> ToolResult:
    try:
        resolved = _check_path(path, allow_write=True)
        if not resolved.exists():
            return ToolResult(content=f"File not found: {path}", is_error=True)

        text = resolved.read_text(encoding="utf-8")

        # Try exact match first; on failure, try with normalized quotes
        if old_string in text:
            match_string = old_string
        else:
            normalized_text = _normalize_quotes(text)
            normalized_old = _normalize_quotes(old_string)
            if normalized_old not in normalized_text:
                return ToolResult(content=f"String not found in {path}", is_error=True)
            # Find the actual string in the file that matches after normalization
            idx = normalized_text.index(normalized_old)
            match_string = text[idx : idx + len(normalized_old)]

        count = text.count(match_string)
        if count > 1:
            return ToolResult(
                content=f"String found {count} times in {path}; must be unique",
                is_error=True,
            )

        resolved.write_text(text.replace(match_string, new_string), encoding="utf-8")
        return ToolResult(content=f"Replaced text in {resolved}")
    except PermissionError as exc:
        return ToolResult(content=str(exc), is_error=True)
    except Exception as exc:  # pragma: no cover - defensive
        return ToolResult(content=f"Error editing file: {exc}", is_error=True)


edit_file_tool.execute = _edit_file
