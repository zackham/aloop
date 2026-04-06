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


def validate_subagent_config(config: dict) -> list[str]:
    """Validate spawnable_modes / subagent_eligible / can_fork consistency.

    Returns a list of human-readable error messages. Empty list = valid.

    Checks:
    - Every mode listed in any spawnable_modes must exist in modes.
    - Every mode listed in any spawnable_modes must have subagent_eligible: true.
    - subagent_eligible must be a bool.
    - spawnable_modes must be a list of strings.
    - can_fork must be a bool.
    """
    errors: list[str] = []
    modes = config.get("modes", {})
    if not isinstance(modes, dict):
        return errors

    for mode_name, mode_cfg in modes.items():
        if not isinstance(mode_cfg, dict):
            continue

        if "subagent_eligible" in mode_cfg:
            if not isinstance(mode_cfg["subagent_eligible"], bool):
                errors.append(
                    f"mode {mode_name!r}: subagent_eligible must be a bool"
                )

        if "can_fork" in mode_cfg:
            if not isinstance(mode_cfg["can_fork"], bool):
                errors.append(
                    f"mode {mode_name!r}: can_fork must be a bool"
                )

        sm = mode_cfg.get("spawnable_modes")
        if sm is None:
            continue
        if not isinstance(sm, list) or not all(isinstance(x, str) for x in sm):
            errors.append(
                f"mode {mode_name!r}: spawnable_modes must be a list of strings"
            )
            continue

        for target in sm:
            if target not in modes:
                errors.append(
                    f"mode {mode_name!r}: spawnable_modes references "
                    f"unknown mode {target!r}"
                )
                continue
            target_cfg = modes[target]
            if not isinstance(target_cfg, dict):
                continue
            if not target_cfg.get("subagent_eligible", False):
                errors.append(
                    f"mode {mode_name!r}: spawnable_modes references "
                    f"{target!r} which is not subagent_eligible"
                )

    return errors


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
