"""aloop — project-independent agent loop harness.

An agent loop with tool access, skills, hooks, and a configurable
system prompt. Projects define themselves via AGENTS.md and .aloop/ config.
"""

from pathlib import Path
import os

__version__ = "1.0.0"

# Project root discovery: ALOOP_PROJECT_ROOT env var, or CWD
def get_project_root() -> Path:
    env = os.environ.get("ALOOP_PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()

from .types import EventType, InferenceEvent, RunResult, InferenceResult, InferenceError, ModeConflictError
from .tools_base import ToolDef, ToolResult, ToolRejected, tool, ToolParam
from .agent_backend import ALoop, AgentLoopBackend
from .acp import serve_acp
from .config import LoopConfig
from .utils import strip_json_comments, load_jsonc

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
    "strip_json_comments",
    "load_jsonc",
]
