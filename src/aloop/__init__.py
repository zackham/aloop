"""aloop — project-independent agent loop harness.

An agent loop with tool access, skills, hooks, and a configurable
system prompt. Projects define themselves via AGENTS.md and .aloop/ config.
"""

from pathlib import Path
import os

__version__ = "0.1.0"

# Project root discovery: ALOOP_PROJECT_ROOT env var, or CWD
def get_project_root() -> Path:
    env = os.environ.get("ALOOP_PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()

from .types import EventType, InferenceEvent, InferenceResult, InferenceError
from .tools_base import ToolDef, ToolResult
from .agent_backend import AgentLoopBackend
from .acp import serve_acp

__all__ = [
    "get_project_root",
    "AgentLoopBackend",
    "EventType",
    "InferenceEvent",
    "InferenceResult",
    "InferenceError",
    "ToolDef",
    "ToolResult",
    "serve_acp",
]
