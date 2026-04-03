"""Built-in tools for the aloop agent loop.

Generic tools only. Project-specific tools are added via hooks (register_tools).
"""

from __future__ import annotations

from ..tools_base import ToolDef, ToolResult, tool
from .files import edit_file_tool, read_file_tool, write_file_tool
from .shell import bash_tool
from .skills import load_skill_tool

# Core tools available to all tasks
CORE_TOOLS = [read_file_tool, load_skill_tool]
ANALYSIS_TOOLS = CORE_TOOLS + [bash_tool, write_file_tool, edit_file_tool]

__all__ = [
    "ToolDef",
    "ToolResult",
    "tool",
    "read_file_tool",
    "write_file_tool",
    "edit_file_tool",
    "bash_tool",
    "load_skill_tool",
    "CORE_TOOLS",
    "ANALYSIS_TOOLS",
]
