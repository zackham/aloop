"""Declarative permission enforcement for aloop.

Security model:
  - No permissions config = no restrictions (yolo default).
  - Tool sets are the real security boundary. Modes without bash: path
    restrictions are meaningful. Modes with bash: path restrictions are
    cosmetic (bash can bypass everything).
  - Hardcoded deny list is non-overridable catastrophe prevention, not security.

This module provides a built-in before_tool hook at priority 0 that enforces:
  1. Tool is in the mode's tool set
  2. Path deny globs (read + write) for file tools
  3. Project containment for file tools
  4. Hardcoded safety net (non-overridable)
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from .tools_base import ToolRejected

# --- Hardcoded safety nets (non-overridable) ---

HARDCODED_DENY_WRITE = [".git/**", ".aloop/config.json"]
# Each entry is (prefix, must_be_exact_or_followed_by_space_or_end).
# We check startswith but also verify the match isn't a prefix of a longer path.
_HARDCODED_DENY_BASH_EXACT = [
    "rm -rf /",       # Matches "rm -rf /" but not "rm -rf /tmp/foo"
    "rm -rf ~",       # Matches "rm -rf ~" but not "rm -rf ~/old-backup"
    "rm -rf /*",      # Matches "rm -rf /*"
    ":(){ :|:& };:",  # Fork bomb
]
_HARDCODED_DENY_BASH_PREFIX = [
    "mkfs ",          # mkfs followed by anything
    "mkfs.",          # mkfs.ext4 etc
    "dd if=",         # dd with input
]

# Tools that operate on file paths
_READ_TOOLS = {"read_file", "grep", "find", "ls"}
_WRITE_TOOLS = {"write_file", "edit_file"}
_FILE_TOOLS = _READ_TOOLS | _WRITE_TOOLS


class PermissionDenied(ToolRejected):
    """Permission check failed. Distinct from ToolRejected so agents
    can distinguish 'not allowed' from 'hook rejected for other reasons'."""
    pass


def _extract_path(tool_name: str, args: dict) -> str | None:
    """Extract the file path from a tool call's arguments."""
    if tool_name in ("read_file", "write_file", "edit_file", "ls"):
        return args.get("path")
    if tool_name in ("grep", "find"):
        return args.get("path")  # Optional, defaults to project root
    return None


def _matches_any_glob(path_str: str, patterns: list[str]) -> bool:
    """Check if a path matches any glob pattern."""
    for pattern in patterns:
        if fnmatch.fnmatch(path_str, pattern):
            return True
    return False


def _make_relative(path_str: str, root: Path) -> str | None:
    """Make a path relative to root. Returns None if outside root."""
    try:
        p = Path(path_str)
        if not p.is_absolute():
            p = root / p
        p = p.resolve()
        rel = p.relative_to(root)
        return str(rel)
    except (ValueError, OSError):
        return None


def check_permissions(
    tool_name: str,
    args: dict,
    *,
    allowed_tools: set[str] | None = None,
    permissions: dict | None = None,
    project_root: Path | None = None,
) -> None:
    """Check if a tool call is permitted. Raises PermissionDenied if not.

    Args:
        tool_name: Name of the tool being called.
        args: Arguments to the tool.
        allowed_tools: Set of tool names allowed in this mode. None = all allowed.
        permissions: The resolved permissions config dict. None = no restrictions.
        project_root: Project root for path containment.
    """
    # 1. Hardcoded bash safety net (always active, non-overridable)
    if tool_name == "bash":
        command = args.get("command", "").strip()
        # Exact matches (must match the full command or be followed by whitespace/end)
        for deny in _HARDCODED_DENY_BASH_EXACT:
            if command == deny or command == deny.rstrip():
                raise PermissionDenied(f"Command blocked (safety): {deny}")
        # Prefix matches (command starts with this prefix)
        for deny in _HARDCODED_DENY_BASH_PREFIX:
            if command.startswith(deny):
                raise PermissionDenied(f"Command blocked (safety): {deny}")

    # 2. Tool set check
    if allowed_tools is not None and tool_name not in allowed_tools:
        raise PermissionDenied(f"Tool '{tool_name}' is not available in this mode")

    # 3. Path checks (only for file tools)
    if tool_name not in _FILE_TOOLS:
        return

    raw_path = _extract_path(tool_name, args)
    if not raw_path:
        # No path arg (e.g. grep/find with default ".") — allow
        return

    if project_root is None:
        from . import get_project_root
        project_root = get_project_root()

    # Resolve to relative path for glob matching
    rel_path = _make_relative(raw_path, project_root)

    # 3a. Hardcoded write denies (always active, non-overridable)
    if tool_name in _WRITE_TOOLS and rel_path is not None:
        for pattern in HARDCODED_DENY_WRITE:
            if fnmatch.fnmatch(rel_path, pattern):
                raise PermissionDenied(f"Write to '{rel_path}' is blocked (safety)")

    # No further checks if no permissions config
    if not permissions:
        return
    paths_config = permissions.get("paths", {})
    if not paths_config:
        return

    # 3b. Deny globs (apply to ALL file operations — read and write)
    deny_patterns = paths_config.get("deny", [])
    if deny_patterns and rel_path is not None:
        if _matches_any_glob(rel_path, deny_patterns):
            raise PermissionDenied(f"Access to '{rel_path}' denied by permissions config")

    # 3c. Project containment
    allow_outside = paths_config.get("allow_outside_project", True)  # Default: allow
    if not allow_outside:
        if rel_path is None:
            # Path is outside project root
            additional = paths_config.get("additional_dirs", [])
            abs_path = Path(raw_path).resolve()
            allowed = False
            for extra_dir in additional:
                extra = Path(extra_dir).expanduser().resolve()
                try:
                    abs_path.relative_to(extra)
                    allowed = True
                    break
                except ValueError:
                    continue
            if not allowed:
                raise PermissionDenied(
                    f"Access to '{raw_path}' denied: outside project root "
                    f"(allow_outside_project is false)"
                )

    # 3d. Write path restrictions (only for write tools)
    if tool_name in _WRITE_TOOLS and rel_path is not None:
        write_patterns = paths_config.get("write", [])
        if write_patterns:
            if not _matches_any_glob(rel_path, write_patterns):
                raise PermissionDenied(
                    f"Write to '{rel_path}' denied: not in allowed write paths"
                )
