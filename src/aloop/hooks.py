"""Hook execution for the aloop agent loop.

Thin integration between .aloop/hooks/ and agent_backend.py.
All functions are no-ops if no hooks are configured — never crashes.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

def _default_root() -> Path:
    from . import get_project_root
    return get_project_root()
_hooks_mod = None


def _ensure_discovered(root: Path | None = None) -> Any:
    """Import and discover hooks. Returns the hooks module or None."""
    global _hooks_mod
    if _hooks_mod is not None:
        return _hooks_mod

    root = root or _default_root()
    hooks_init = root / ".aloop" / "hooks" / "__init__.py"
    if not hooks_init.exists():
        return None

    try:
        import importlib.util
        import sys as _sys

        spec = importlib.util.spec_from_file_location("aloop_hooks", hooks_init)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        _sys.modules["aloop_hooks"] = mod  # register before exec so @dataclass works
        spec.loader.exec_module(mod)
        mod.discover_hooks(hooks_init.parent)
        _hooks_mod = mod
        return mod
    except Exception:
        log.debug("Hook discovery failed", exc_info=True)
        return None


def run_before_tool(
    name: str, args: dict, root: Path | None = None, **context
) -> dict:
    """Run before_tool hooks. Returns {"allow": True} by default."""
    mod = _ensure_discovered(root)
    if not mod:
        return {"allow": True}

    for entry in mod.get_hooks("before_tool"):
        try:
            result = entry.fn(name, args, **context)
            if inspect.isawaitable(result):
                import asyncio
                result = asyncio.get_event_loop().run_until_complete(result)
            if isinstance(result, dict) and not result.get("allow", True):
                return result
            if isinstance(result, dict) and "modified_args" in result:
                args = result["modified_args"]
        except Exception:
            log.warning("Hook %s failed", entry.name, exc_info=True)

    return {"allow": True, "args": args}


def run_after_tool(
    name: str, args: dict, result: str, root: Path | None = None, **context
) -> dict:
    """Run after_tool hooks. Returns {"modified_result": result} by default."""
    mod = _ensure_discovered(root)
    if not mod:
        return {"modified_result": result}

    for entry in mod.get_hooks("after_tool"):
        try:
            hook_result = entry.fn(name, args, result, **context)
            if isinstance(hook_result, dict) and "modified_result" in hook_result:
                result = hook_result["modified_result"]
        except Exception:
            log.warning("Hook %s failed", entry.name, exc_info=True)

    return {"modified_result": result}


def run_gather_context(
    task_type: str, root: Path | None = None, **kwargs
) -> str:
    """Run gather_context hooks, concatenate results."""
    mod = _ensure_discovered(root)
    if not mod:
        return ""

    parts: list[str] = []
    for entry in mod.get_hooks("gather_context"):
        try:
            result = entry.fn(task_type, **kwargs)
            if isinstance(result, str) and result.strip():
                parts.append(result.strip())
        except Exception:
            log.warning("Hook %s failed", entry.name, exc_info=True)

    return "\n\n".join(parts)


def run_register_tools(root: Path | None = None) -> list:
    """Run register_tools hooks, collect ToolDefs."""
    mod = _ensure_discovered(root)
    if not mod:
        return []

    tools: list = []
    for entry in mod.get_hooks("register_tools"):
        try:
            result = entry.fn()
            if isinstance(result, list):
                tools.extend(result)
        except Exception:
            log.warning("Hook %s failed", entry.name, exc_info=True)

    return tools


def reset(root: Path | None = None):
    """Reset hook state (for testing)."""
    global _hooks_mod
    if _hooks_mod:
        _hooks_mod.reset()
    _hooks_mod = None
