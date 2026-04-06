"""aloop — project-independent agent loop harness.

An agent loop with tool access, skills, hooks, and a configurable
system prompt. Projects define themselves via AGENTS.md and .aloop/ config.
"""

from pathlib import Path
import os

__version__ = "0.6.0"

# Project root discovery: ALOOP_PROJECT_ROOT env var, or CWD
def get_project_root() -> Path:
    env = os.environ.get("ALOOP_PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()

from .types import EventType, InferenceEvent, RunResult, InferenceResult, InferenceError, ModeConflictError
from .tools_base import ToolDef, ToolResult, ToolRejected, tool, ToolParam
from .permissions import PermissionDenied
from .agent_backend import ALoop, AgentLoopBackend
from .acp import serve_acp
from .config import LoopConfig, validate_subagent_config
from .utils import strip_json_comments, load_jsonc
from .tools import CODING_TOOLS, READONLY_TOOLS, ALL_TOOLS
from .agent_result import AgentResult, FORK_BOILERPLATE, extract_partial_result
from .executor import AgentExecutor, AgentExecutionHandle, InProcessExecutor

__all__ = [
    "get_project_root",
    "ALoop",
    "AgentLoopBackend",  # deprecated alias
    "EventType",
    "InferenceEvent",
    "RunResult",
    "InferenceResult",  # deprecated alias
    "InferenceError",
    "ModeConflictError",
    "ToolDef",
    "ToolResult",
    "ToolRejected",
    "tool",
    "ToolParam",
    "serve_acp",
    "LoopConfig",
    "PermissionDenied",
    "CODING_TOOLS",
    "READONLY_TOOLS",
    "ALL_TOOLS",
    "strip_json_comments",
    "load_jsonc",
    # Subagents (v0.6.0)
    "AgentResult",
    "AgentExecutor",
    "AgentExecutionHandle",
    "InProcessExecutor",
    "FORK_BOILERPLATE",
    "extract_partial_result",
    "validate_subagent_config",
]
