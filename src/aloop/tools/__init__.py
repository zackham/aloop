"""Built-in tools for the aloop agent loop.

Generic tools only. Project-specific tools are added via hooks (register_tools).

Tool sets:
  CODING_TOOLS  — default set. read, write, edit, bash, skill. Full access.
  READONLY_TOOLS — safe exploration. read, grep, find, ls, skill. No shell, no writes.
  ALL_TOOLS     — everything. Union of coding + readonly tools.
"""

from __future__ import annotations

from ..tools_base import ToolDef, ToolResult, tool
from .files import edit_file_tool, read_file_tool, write_file_tool
from .search import find_tool, grep_tool, ls_tool
from .shell import bash_tool
from .skills import load_skill_tool

# Core tools available to all tasks (minimal)
CORE_TOOLS = [read_file_tool, load_skill_tool]

# Default set — same as before. bash is god mode.
CODING_TOOLS = [read_file_tool, write_file_tool, edit_file_tool, bash_tool, load_skill_tool]

# Safe exploration — no shell, no writes. Modeled on Pi's readOnlyTools.
READONLY_TOOLS = [read_file_tool, grep_tool, find_tool, ls_tool, load_skill_tool]

# Everything
ALL_TOOLS = [read_file_tool, write_file_tool, edit_file_tool, bash_tool,
             grep_tool, find_tool, ls_tool, load_skill_tool]

# Backwards compat — ANALYSIS_TOOLS was the old name for the default set
ANALYSIS_TOOLS = CODING_TOOLS

__all__ = [
    "ToolDef",
    "ToolResult",
    "tool",
    "read_file_tool",
    "write_file_tool",
    "edit_file_tool",
    "bash_tool",
    "grep_tool",
    "find_tool",
    "ls_tool",
    "load_skill_tool",
    "CORE_TOOLS",
    "CODING_TOOLS",
    "READONLY_TOOLS",
    "ALL_TOOLS",
    "ANALYSIS_TOOLS",
]
