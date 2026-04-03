"""Hook execution for the aloop agent loop.

Thin integration between hook directories and agent_backend.py.
All functions are no-ops if no hooks are configured — never crashes.

Hook loading order:
  1. Global: ~/.aloop/hooks/ (if exists)
  2. Project: .aloop/hooks/ (if exists)
  Both run. Global first, project second.
  Same-name hook file: project wins (replaces global's version).

Hooks listed in config disabled_hooks are skipped entirely.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any

from .tools_base import ToolRejected, ToolResult

log = logging.getLogger(__name__)

def _default_root() -> Path:
    from . import get_project_root
    return get_project_root()

_hooks_mod = None


def _load_hooks_module(hooks_init: Path) -> Any:
    """Import a hooks __init__.py and return the module, or None."""
    if not hooks_init.exists():
        return None
    try:
        import importlib.util
        import sys as _sys

        # Use unique module name per path to avoid collisions
        mod_name = f"aloop_hooks_{hash(str(hooks_init))}"
        spec = importlib.util.spec_from_file_location(mod_name, hooks_init)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        _sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        log.debug("Hook module load failed: %s", hooks_init, exc_info=True)
        return None


def _ensure_discovered(root: Path | None = None) -> Any:
    """Import and discover hooks from global + project dirs.

    Returns the project hooks module (or global if no project), or None.
    The combined hook registry lives on the returned module.
    """
    global _hooks_mod
    if _hooks_mod is not None:
        return _hooks_mod

    root = root or _default_root()

    # Load config for disabled_hooks
    from .system_prompt import _load_aloop_config
    config = _load_aloop_config(root)
    disabled_hooks = set(config.get("disabled_hooks", []))

    global_hooks_init = Path.home() / ".aloop" / "hooks" / "__init__.py"
    project_hooks_init = root / ".aloop" / "hooks" / "__init__.py"

    global_mod = _load_hooks_module(global_hooks_init)
    project_mod = _load_hooks_module(project_hooks_init)

    if not global_mod and not project_mod:
        return None

    # We use the project module as the primary (or global if no project).
    # Discover hooks in each, then merge.
    primary_mod = project_mod or global_mod

    # Discover hooks from global dir
    global_hooks_by_file: dict[str, list] = {}
    if global_mod and hasattr(global_mod, "discover_hooks"):
        try:
            global_mod.discover_hooks(global_hooks_init.parent)
            # Collect hooks keyed by their source filename
            if hasattr(global_mod, "get_hooks"):
                for point in ["before_tool", "after_tool", "gather_context", "register_tools", "on_loop_start", "on_loop_end", "on_turn_start", "on_turn_end", "on_pre_compaction", "on_post_compaction"]:
                    for entry in global_mod.get_hooks(point):
                        fname = getattr(entry, "source_file", None) or entry.name
                        if fname not in global_hooks_by_file:
                            global_hooks_by_file[fname] = []
                        global_hooks_by_file[fname].append((point, entry))
        except Exception:
            log.debug("Global hook discovery failed", exc_info=True)

    # Discover hooks from project dir
    project_hook_files: set[str] = set()
    if project_mod and hasattr(project_mod, "discover_hooks"):
        try:
            project_mod.discover_hooks(project_hooks_init.parent)
            # Collect project hook filenames to know what overrides global
            if hasattr(project_mod, "get_hooks"):
                for point in ["before_tool", "after_tool", "gather_context", "register_tools", "on_loop_start", "on_loop_end", "on_turn_start", "on_turn_end", "on_pre_compaction", "on_post_compaction"]:
                    for entry in project_mod.get_hooks(point):
                        fname = getattr(entry, "source_file", None) or entry.name
                        project_hook_files.add(fname)
        except Exception:
            log.debug("Project hook discovery failed", exc_info=True)

    # If we have both global and project, merge: global hooks whose file
    # is NOT overridden by project and NOT disabled get prepended.
    if global_mod and project_mod and hasattr(primary_mod, "_registry"):
        # Inject non-overridden global hooks before project hooks
        for fname, entries in global_hooks_by_file.items():
            if fname in project_hook_files:
                continue  # project overrides this file
            if fname in disabled_hooks:
                continue
            for point, entry in entries:
                if hasattr(primary_mod, "_registry"):
                    if point not in primary_mod._registry:
                        primary_mod._registry[point] = []
                    primary_mod._registry[point].insert(0, entry)
    elif global_mod and not project_mod:
        # Only global — just use it as primary
        primary_mod = global_mod

    # Apply disabled_hooks filter to final registry
    if disabled_hooks and hasattr(primary_mod, "_registry"):
        for point in list(primary_mod._registry.keys()):
            primary_mod._registry[point] = [
                e for e in primary_mod._registry[point]
                if (getattr(e, "source_file", None) or e.name) not in disabled_hooks
            ]

    import sys as _sys
    _sys.modules["aloop_hooks"] = primary_mod
    _hooks_mod = primary_mod
    return primary_mod


def run_before_tool(
    name: str, args: dict, root: Path | None = None, **context
) -> dict:
    """Run before_tool hooks. Returns {"allow": True} by default.

    Return contract for individual hooks:
    - Return None → proceed (same as allow=True)
    - Raise ToolRejected(reason) → cancel, reason passed to model as error
    - Return ToolResult → short-circuit with this result (mock/cache)
    - Return {"allow": False, "reason": ...} → cancel (backward compat)
    - Return {"allow": True, "modified_args": ...} → proceed with modified args
    """
    mod = _ensure_discovered(root)
    if not mod:
        return {"allow": True}

    for entry in mod.get_hooks("before_tool"):
        try:
            result = entry.fn(name, args, **context)
            if inspect.isawaitable(result):
                import asyncio
                result = asyncio.get_event_loop().run_until_complete(result)
            # None → proceed
            if result is None:
                continue
            # ToolResult → short-circuit
            if isinstance(result, ToolResult):
                return {"allow": False, "tool_result": result}
            # Dict-based contract (backward compat)
            if isinstance(result, dict) and not result.get("allow", True):
                return result
            if isinstance(result, dict) and "modified_args" in result:
                args = result["modified_args"]
        except ToolRejected as exc:
            return {"allow": False, "reason": exc.reason}
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


def run_on_loop_start(context: dict, root: Path | None = None) -> None:
    """Run on_loop_start hooks. Called at start of stream()."""
    mod = _ensure_discovered(root)
    if not mod:
        return

    for entry in mod.get_hooks("on_loop_start"):
        try:
            result = entry.fn(context)
            if inspect.isawaitable(result):
                import asyncio
                asyncio.get_event_loop().run_until_complete(result)
        except Exception:
            log.warning("Hook %s failed", entry.name, exc_info=True)


def run_on_loop_end(context: dict, result: dict, root: Path | None = None) -> None:
    """Run on_loop_end hooks. Called at end of stream()."""
    mod = _ensure_discovered(root)
    if not mod:
        return

    for entry in mod.get_hooks("on_loop_end"):
        try:
            r = entry.fn(context, result)
            if inspect.isawaitable(r):
                import asyncio
                asyncio.get_event_loop().run_until_complete(r)
        except Exception:
            log.warning("Hook %s failed", entry.name, exc_info=True)


def run_on_turn_start(context: dict, root: Path | None = None) -> None:
    """Run on_turn_start hooks. Called at start of each turn."""
    mod = _ensure_discovered(root)
    if not mod:
        return

    for entry in mod.get_hooks("on_turn_start"):
        try:
            result = entry.fn(context)
            if inspect.isawaitable(result):
                import asyncio
                asyncio.get_event_loop().run_until_complete(result)
        except Exception:
            log.warning("Hook %s failed", entry.name, exc_info=True)


def run_on_turn_end(context: dict, result: dict, root: Path | None = None) -> None:
    """Run on_turn_end hooks. Called at end of each turn."""
    mod = _ensure_discovered(root)
    if not mod:
        return

    for entry in mod.get_hooks("on_turn_end"):
        try:
            r = entry.fn(context, result)
            if inspect.isawaitable(r):
                import asyncio
                asyncio.get_event_loop().run_until_complete(r)
        except Exception:
            log.warning("Hook %s failed", entry.name, exc_info=True)


def run_on_pre_compaction(context: dict, root: Path | None = None) -> str | None:
    """Run on_pre_compaction hooks. Called before compaction.

    Hooks may return extra instructions (strings) to append to the
    compaction prompt. Returns concatenated instructions or None.
    """
    mod = _ensure_discovered(root)
    if not mod:
        return None

    parts: list[str] = []
    for entry in mod.get_hooks("on_pre_compaction"):
        try:
            result = entry.fn(context)
            if inspect.isawaitable(result):
                import asyncio
                result = asyncio.get_event_loop().run_until_complete(result)
            if isinstance(result, str) and result.strip():
                parts.append(result.strip())
        except Exception:
            log.warning("Hook %s failed", entry.name, exc_info=True)

    return "\n\n".join(parts) if parts else None


def run_on_post_compaction(context: dict, root: Path | None = None) -> None:
    """Run on_post_compaction hooks. Called after compaction."""
    mod = _ensure_discovered(root)
    if not mod:
        return

    for entry in mod.get_hooks("on_post_compaction"):
        try:
            result = entry.fn(context)
            if inspect.isawaitable(result):
                import asyncio
                asyncio.get_event_loop().run_until_complete(result)
        except Exception:
            log.warning("Hook %s failed", entry.name, exc_info=True)


def get_discovered_hooks(root: Path | None = None) -> dict[str, list[str]]:
    """Return discovered hook names grouped by scope (global/project).

    Returns {"global": [...], "project": [...]} for display in config show.
    """
    root = root or _default_root()
    result: dict[str, list[str]] = {"global": [], "project": []}

    global_dir = Path.home() / ".aloop" / "hooks"
    project_dir = root / ".aloop" / "hooks"

    for scope, d in [("global", global_dir), ("project", project_dir)]:
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if f.suffix == ".py" and not f.name.startswith("_"):
                result[scope].append(f.stem)

    return result


def reset(root: Path | None = None):
    """Reset hook state (for testing)."""
    global _hooks_mod
    if _hooks_mod:
        if hasattr(_hooks_mod, "reset"):
            _hooks_mod.reset()
    _hooks_mod = None
