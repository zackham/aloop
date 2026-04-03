"""Skill bridge: load_skill tool + skill listing builder.

Progressive skill loading:
- Short listing of all skills injected as context (~1% of context budget)
- load_skill tool loads full SKILL.md content on demand
- Model decides when to invoke based on description

Skill discovery order (merged, not first-match):
  Global: ~/.aloop/skills/
  Project: .aloop/skills/ > .agents/skills/ > .claude/skills/

All directories are scanned. Skills from all found directories are merged.
On name collision, project overrides global; higher-priority project dir
overrides lower-priority.
"""

from __future__ import annotations

from pathlib import Path

from ..tools_base import ToolDef, ToolResult

from .. import get_project_root


def _find_all_skills_dirs() -> list[Path]:
    """Find all skills directories, in priority order (highest first).

    Project dirs first (highest to lowest priority), then global.
    """
    root = get_project_root()
    dirs: list[Path] = []

    # Project skill dirs (highest priority first)
    for candidate in [
        root / ".aloop" / "skills",
        root / ".agents" / "skills",
        root / ".claude" / "skills",
    ]:
        if candidate.is_dir():
            dirs.append(candidate)

    # Global skills (lowest priority)
    global_dir = Path.home() / ".aloop" / "skills"
    if global_dir.is_dir():
        dirs.append(global_dir)

    return dirs


MAX_LISTING_DESC_CHARS = 250


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract YAML frontmatter from SKILL.md content."""
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    fm: dict[str, str] = {}
    for line in text[3:end].strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip()] = value.strip().strip('"').strip("'")
    return fm


def _discover_skills(
    skills_dirs: list[Path] | None = None,
    disabled_skills: set[str] | None = None,
) -> dict[str, dict]:
    """Scan skills directories and return {name: {description, path, source}} for each skill.

    Directories are scanned in priority order. First occurrence of a name wins
    (higher-priority directory overrides lower).
    """
    if skills_dirs is None:
        skills_dirs = _find_all_skills_dirs()
    if disabled_skills is None:
        disabled_skills = _load_disabled_skills()

    skills: dict[str, dict] = {}

    for skills_dir in skills_dirs:
        if not skills_dir.is_dir():
            continue

        for skill_dir in sorted(skills_dir.iterdir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                text = skill_md.read_text(encoding="utf-8")
            except OSError:
                continue

            fm = _parse_frontmatter(text)
            name = fm.get("name") or skill_dir.name
            desc = fm.get("description", "")

            # Skip disabled skills
            if name in disabled_skills:
                continue

            # First occurrence wins (higher priority dir first)
            if name in skills:
                continue

            skills[name] = {
                "description": desc,
                "path": str(skill_md),
                "source": str(skills_dir),
            }

    return skills


def _load_disabled_skills() -> set[str]:
    """Load disabled_skills from merged config."""
    try:
        from ..system_prompt import _load_aloop_config
        from .. import get_project_root
        config = _load_aloop_config(get_project_root())
        return set(config.get("disabled_skills", []))
    except Exception:
        return set()


# Cache after first scan
_skill_cache: dict[str, dict] | None = None


def _get_skills() -> dict[str, dict]:
    global _skill_cache
    if _skill_cache is None:
        _skill_cache = _discover_skills()
    return _skill_cache


def build_skill_listing(max_chars: int = 8_000) -> str:
    """Build a short listing of all available skills.

    Returns a string suitable for injection as a system-reminder message.
    Budget defaults to ~8K chars (1% of 200K context at 4 chars/token).
    """
    skills = _get_skills()
    if not skills:
        return ""

    lines: list[str] = []
    used = 0
    for name, info in skills.items():
        desc = info["description"]
        if len(desc) > MAX_LISTING_DESC_CHARS:
            desc = desc[:MAX_LISTING_DESC_CHARS - 3] + "..."
        line = f"- {name}: {desc}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)

    if not lines:
        return ""

    return (
        "The following skills are available via the load_skill tool. "
        "Call load_skill with the skill name to get full instructions.\n\n"
        + "\n".join(lines)
    )


def list_skill_names() -> list[str]:
    """Return sorted list of available skill names."""
    return sorted(_get_skills().keys())


def get_skills_by_source() -> dict[str, list[str]]:
    """Return skills grouped by source directory for config show display.

    Returns {dir_path: [skill_name, ...]} preserving discovery order.
    """
    skills = _get_skills()
    by_source: dict[str, list[str]] = {}
    for name, info in skills.items():
        source = info.get("source", "unknown")
        if source not in by_source:
            by_source[source] = []
        by_source[source].append(name)
    return by_source


# --- Tool definition ---

load_skill_tool = ToolDef(
    name="load_skill",
    description=(
        "Load a skill's full instructions by name. Returns the complete "
        "SKILL.md content. Use when a skill matches the user's request."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "Skill name (e.g. 'checkin', 'now', 'handoff')",
            },
            "args": {
                "type": "string",
                "description": "Optional arguments to pass to the skill",
                "default": "",
            },
        },
        "required": ["skill"],
    },
    execute=None,
)


async def _load_skill(skill: str, args: str = "", **kwargs) -> ToolResult:
    skills = _get_skills()

    if skill not in skills:
        available = ", ".join(sorted(skills.keys()))
        return ToolResult(
            content=f"Unknown skill: '{skill}'. Available: {available}",
            is_error=True,
        )

    skill_path = Path(skills[skill]["path"])
    try:
        content = skill_path.read_text(encoding="utf-8")
    except OSError as e:
        return ToolResult(content=f"Error reading skill: {e}", is_error=True)

    # Variable substitution
    content = content.replace("$ARGUMENTS", args)
    content = content.replace("${ARGUMENTS}", args)
    skill_dir = str(skill_path.parent)
    content = content.replace("${CLAUDE_SKILL_DIR}", skill_dir)

    return ToolResult(content=content)


load_skill_tool.execute = _load_skill
