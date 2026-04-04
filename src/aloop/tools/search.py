"""Read-only exploration tools: grep, find, ls.

These tools provide safe codebase exploration without shell access.
Modeled on pi-coding-agent v0.64.0's readOnlyTools.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from ..tools_base import ToolDef, ToolResult
from .. import get_project_root

MAX_OUTPUT_BYTES = 50 * 1024  # 50KB, matches Pi
GREP_MAX_LINE_LENGTH = 500
GREP_DEFAULT_LIMIT = 100
FIND_DEFAULT_LIMIT = 1000
LS_DEFAULT_LIMIT = 500


def _resolve_search_path(path_str: str | None) -> Path:
    """Resolve a search path relative to project root."""
    root = get_project_root()
    if not path_str or path_str == ".":
        return root
    p = Path(path_str)
    if not p.is_absolute():
        p = root / p
    return p.resolve()


def _truncate_output(text: str, max_bytes: int = MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    """Truncate text to max_bytes, cutting at line boundaries."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    # Cut at byte limit, then back up to last newline
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    last_nl = truncated.rfind("\n")
    if last_nl > 0:
        truncated = truncated[:last_nl]
    return truncated, True


def _truncate_line(line: str, max_chars: int = GREP_MAX_LINE_LENGTH) -> str:
    """Truncate a single line, appending indicator if truncated."""
    if len(line) <= max_chars:
        return line
    return line[:max_chars] + "... [truncated]"


# --- grep tool ---

grep_tool = ToolDef(
    name="grep",
    description=(
        "Search file contents for a pattern using ripgrep. "
        "Returns matching lines with file paths and line numbers. "
        "Respects .gitignore. Use for exploring code without shell access."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Search pattern (regex or literal string)"},
            "path": {"type": "string", "description": "Directory or file to search (default: project root)"},
            "glob": {"type": "string", "description": "Filter files by glob pattern, e.g. '*.py' or '**/*.ts'"},
            "ignore_case": {"type": "boolean", "description": "Case-insensitive search (default: false)"},
            "literal": {"type": "boolean", "description": "Treat pattern as literal string instead of regex (default: false)"},
            "context": {"type": "integer", "description": "Lines of context before and after each match (default: 0)"},
            "limit": {"type": "integer", "description": "Maximum number of matches (default: 100)"},
        },
        "required": ["pattern"],
    },
    execute=None,
)


async def _grep(
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    ignore_case: bool = False,
    literal: bool = False,
    context: int = 0,
    limit: int = GREP_DEFAULT_LIMIT,
) -> ToolResult:
    rg = shutil.which("rg")
    if not rg:
        return ToolResult(
            content="ripgrep (rg) is not installed. Install it: https://github.com/BurntSushi/ripgrep#installation",
            is_error=True,
        )

    search_path = _resolve_search_path(path)
    if not search_path.exists():
        return ToolResult(content=f"Path not found: {path}", is_error=True)

    effective_limit = max(1, int(limit))
    effective_context = max(0, int(context))

    args = [rg, "--line-number", "--color=never", "--hidden", "--no-heading"]
    if ignore_case:
        args.append("--ignore-case")
    if literal:
        args.append("--fixed-strings")
    if glob:
        args.extend(["--glob", glob])
    if effective_context > 0:
        args.extend(["-C", str(effective_context)])
    # Use rg's max-count to limit matches efficiently
    args.extend(["--max-count", str(effective_limit)])
    args.append(pattern)
    args.append(str(search_path))

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        return ToolResult(content="grep timed out after 60s", is_error=True)
    except Exception as exc:
        return ToolResult(content=f"grep error: {exc}", is_error=True)

    # rg exit codes: 0 = matches, 1 = no matches, 2 = error
    if proc.returncode == 1:
        return ToolResult(content="No matches found")
    if proc.returncode == 2:
        err = stderr.decode(errors="replace").strip()
        return ToolResult(content=f"grep error: {err}", is_error=True)

    output = stdout.decode(errors="replace")

    # Relativize paths and truncate long lines
    lines = output.splitlines()
    formatted = []
    match_count = 0
    for line in lines:
        # Context separator lines from rg
        if line == "--":
            formatted.append("--")
            continue
        formatted.append(_truncate_line(line))
        # Count actual match lines (not context lines which use - separator)
        if ":" in line and not line.startswith("-"):
            match_count += 1

    result_text = "\n".join(formatted)
    result_text, was_truncated = _truncate_output(result_text)

    notices = []
    if match_count >= effective_limit:
        notices.append(f"{effective_limit} match limit reached. Use limit={effective_limit * 2} for more, or refine pattern")
    if was_truncated:
        notices.append("Output truncated at 50KB")
    if notices:
        result_text += f"\n\n[{'. '.join(notices)}]"

    return ToolResult(content=result_text)


grep_tool.execute = _grep


# --- find tool ---

find_tool = ToolDef(
    name="find",
    description=(
        "Find files by glob pattern. Returns matching file paths relative to "
        "the search directory. Respects .gitignore. "
        "Use for discovering files without shell access."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '*.py', '**/*.json', 'src/**/*.spec.ts'"},
            "path": {"type": "string", "description": "Directory to search in (default: project root)"},
            "limit": {"type": "integer", "description": "Maximum number of results (default: 1000)"},
        },
        "required": ["pattern"],
    },
    execute=None,
)


async def _find(
    pattern: str,
    path: str | None = None,
    limit: int = FIND_DEFAULT_LIMIT,
) -> ToolResult:
    search_path = _resolve_search_path(path)
    if not search_path.exists():
        return ToolResult(content=f"Path not found: {path}", is_error=True)

    effective_limit = max(1, int(limit))

    # Try fd first (fast, respects .gitignore)
    fd = shutil.which("fd") or shutil.which("fdfind")  # fdfind on Debian/Ubuntu
    if fd:
        args = [
            fd, "--glob", "--color=never", "--hidden",
            "--max-results", str(effective_limit),
            pattern, str(search_path),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            return ToolResult(content="find timed out after 60s", is_error=True)
        except Exception as exc:
            return ToolResult(content=f"find error: {exc}", is_error=True)

        output = stdout.decode(errors="replace").strip()
        if not output:
            return ToolResult(content="No files found matching pattern")

        # Relativize paths
        lines = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            p = Path(line)
            try:
                rel = p.relative_to(search_path)
                lines.append(str(rel))
            except ValueError:
                lines.append(line)

    else:
        # Fallback: Python glob (slower, doesn't respect .gitignore)
        try:
            matches = sorted(search_path.glob(pattern))
        except Exception as exc:
            return ToolResult(content=f"Invalid glob pattern: {exc}", is_error=True)

        # Filter out common noise directories
        skip = {".git", "node_modules", "__pycache__", ".venv", "venv"}
        lines = []
        for m in matches:
            if any(part in skip for part in m.parts):
                continue
            try:
                lines.append(str(m.relative_to(search_path)))
            except ValueError:
                lines.append(str(m))
            if len(lines) >= effective_limit:
                break

    if not lines:
        return ToolResult(content="No files found matching pattern")

    limit_reached = len(lines) >= effective_limit
    result_text = "\n".join(lines)
    result_text, was_truncated = _truncate_output(result_text)

    notices = []
    if limit_reached:
        notices.append(f"{effective_limit} results limit reached. Use limit={effective_limit * 2} for more, or refine pattern")
    if was_truncated:
        notices.append("Output truncated at 50KB")
    if not fd:
        notices.append("Note: fd not installed, using Python glob fallback (slower, doesn't respect .gitignore)")
    if notices:
        result_text += f"\n\n[{'. '.join(notices)}]"

    return ToolResult(content=result_text)


find_tool.execute = _find


# --- ls tool ---

ls_tool = ToolDef(
    name="ls",
    description=(
        "List directory contents. Returns entries sorted alphabetically with "
        "'/' suffix for directories. Includes dotfiles. Single level, no recursion."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to list (default: project root)"},
            "limit": {"type": "integer", "description": "Maximum entries to return (default: 500)"},
        },
    },
    execute=None,
)


async def _ls(
    path: str | None = None,
    limit: int = LS_DEFAULT_LIMIT,
) -> ToolResult:
    dir_path = _resolve_search_path(path)
    if not dir_path.exists():
        return ToolResult(content=f"Path not found: {path}", is_error=True)
    if not dir_path.is_dir():
        return ToolResult(content=f"Not a directory: {path}", is_error=True)

    effective_limit = max(1, int(limit))

    try:
        entries = sorted(os.listdir(dir_path), key=lambda s: s.lower())
    except PermissionError as exc:
        return ToolResult(content=f"Permission denied: {exc}", is_error=True)

    results = []
    for entry in entries:
        if len(results) >= effective_limit:
            break
        full = dir_path / entry
        try:
            suffix = "/" if full.is_dir() else ""
        except (PermissionError, OSError):
            # Broken symlink or permission error — skip silently (Pi behavior)
            continue
        results.append(entry + suffix)

    if not results:
        return ToolResult(content="(empty directory)")

    result_text = "\n".join(results)
    result_text, was_truncated = _truncate_output(result_text)

    notices = []
    if len(results) >= effective_limit:
        notices.append(f"{effective_limit} entries limit reached. Use limit={effective_limit * 2} for more")
    if was_truncated:
        notices.append("Output truncated at 50KB")
    if notices:
        result_text += f"\n\n[{'. '.join(notices)}]"

    return ToolResult(content=result_text)


ls_tool.execute = _ls
