"""Loop configuration dataclass."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .compaction import CompactionSettings


@dataclass
class LoopConfig:
    """Configuration for ALoop behavior.

    Consolidates loop-level settings into a single object
    instead of individual constructor parameters.
    """

    max_iterations: int = 50
    max_session_age: float = 14400.0
    max_session_messages: int = 100
    compaction: CompactionSettings = field(default_factory=CompactionSettings)


def load_mode(mode_name: str, config: dict) -> dict:
    """Load a mode config by name. Returns the mode dict.

    Modes are flat — no inheritance. Omitted fields fall back to
    constructor defaults / global config, NOT to another mode.

    Raises ValueError if mode doesn't exist.
    """
    modes = config.get("modes", {})
    if mode_name not in modes:
        available = list(modes.keys())
        raise ValueError(f"Unknown mode: {mode_name!r}. Available: {available}")
    return dict(modes[mode_name])


def resolve_mode_system_prompt(mode_config: dict, project_root: Path | None = None) -> str | None:
    """Resolve a mode's system prompt from its config.

    If mode has 'system_prompt', return it directly.
    If mode has 'system_prompt_file', read the file relative to project_root.
    Returns None if neither is set.
    """
    if "system_prompt" in mode_config:
        return mode_config["system_prompt"]

    if "system_prompt_file" in mode_config:
        if project_root is None:
            from . import get_project_root
            project_root = get_project_root()
        sp_path = project_root / mode_config["system_prompt_file"]
        try:
            return sp_path.read_text(encoding="utf-8")
        except OSError:
            return None

    return None
