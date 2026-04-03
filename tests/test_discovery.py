"""Tests for Work Session 4: file discovery, global layering, disabled hooks/skills.

Covers:
- Unified instruction file discovery chain
- Skill merging across directories (project + global)
- Global + project hook loading
- Global + project config deep merging
- disabled_hooks / disabled_skills
- Project overrides global on collision
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from aloop.system_prompt import (
    _find_instruction_file,
    _find_agents_md,
    _find_skills_dirs,
    _find_skills_dir,
    _load_aloop_config,
    _deep_merge,
    _build_skill_section,
    _strip_frontmatter,
    build_system_prompt,
    INSTRUCTION_CANDIDATES,
)

from aloop.tools.skills import (
    _discover_skills,
    _find_all_skills_dirs,
    _load_disabled_skills,
)

from aloop.hooks import get_discovered_hooks, reset as hooks_reset


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_instruction_file(root: Path, rel_path: str, content: str = "# Instructions"):
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _make_skill(skills_dir: Path, name: str, frontmatter: str = "", body: str = ""):
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}---\n{body}")


def _make_config(root: Path, config: dict, *, global_config: bool = False):
    if global_config:
        # Global config lives at ~/.aloop/config.json
        # When global_config=True, root is the fake home dir
        p = root / ".aloop" / "config.json"
    else:
        p = root / ".aloop" / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(config))


# ---------------------------------------------------------------------------
# Unified instruction file discovery
# ---------------------------------------------------------------------------


class TestInstructionDiscovery:
    """Test the unified instruction file discovery chain."""

    def test_discovery_chain_order(self):
        """INSTRUCTION_CANDIDATES has the correct order."""
        assert INSTRUCTION_CANDIDATES == [
            "ALOOP.md",
            "AGENTS.md",
            ".agents/AGENTS.md",
            "CLAUDE.md",
            ".claude/CLAUDE.md",
        ]

    def test_aloop_md_wins_over_agents(self, tmp_path):
        _make_instruction_file(tmp_path, "ALOOP.md", "# ALOOP")
        _make_instruction_file(tmp_path, "AGENTS.md", "# AGENTS")
        found, skipped = _find_instruction_file(tmp_path)
        assert found == tmp_path / "ALOOP.md"
        assert tmp_path / "AGENTS.md" in skipped

    def test_agents_md_wins_over_claude(self, tmp_path):
        _make_instruction_file(tmp_path, "AGENTS.md", "# AGENTS")
        _make_instruction_file(tmp_path, "CLAUDE.md", "# CLAUDE")
        found, skipped = _find_instruction_file(tmp_path)
        assert found == tmp_path / "AGENTS.md"
        assert tmp_path / "CLAUDE.md" in skipped

    def test_dot_agents_wins_over_claude(self, tmp_path):
        _make_instruction_file(tmp_path, ".agents/AGENTS.md", "# AGENTS")
        _make_instruction_file(tmp_path, "CLAUDE.md", "# CLAUDE")
        found, skipped = _find_instruction_file(tmp_path)
        assert found == tmp_path / ".agents" / "AGENTS.md"
        assert tmp_path / "CLAUDE.md" in skipped

    def test_claude_md_wins_over_dot_claude(self, tmp_path):
        _make_instruction_file(tmp_path, "CLAUDE.md", "# CLAUDE")
        _make_instruction_file(tmp_path, ".claude/CLAUDE.md", "# DOT CLAUDE")
        found, skipped = _find_instruction_file(tmp_path)
        assert found == tmp_path / "CLAUDE.md"
        assert tmp_path / ".claude" / "CLAUDE.md" in skipped

    def test_dot_claude_only(self, tmp_path):
        _make_instruction_file(tmp_path, ".claude/CLAUDE.md", "# DOT CLAUDE")
        found, skipped = _find_instruction_file(tmp_path)
        assert found == tmp_path / ".claude" / "CLAUDE.md"
        assert skipped == []

    def test_no_instruction_file(self, tmp_path):
        found, skipped = _find_instruction_file(tmp_path)
        assert found is None
        assert skipped == []

    def test_all_five_present(self, tmp_path):
        for c in INSTRUCTION_CANDIDATES:
            _make_instruction_file(tmp_path, c, f"# {c}")
        found, skipped = _find_instruction_file(tmp_path)
        assert found == tmp_path / "ALOOP.md"
        assert len(skipped) == 4

    def test_find_agents_md_uses_unified_chain(self, tmp_path):
        """_find_agents_md now uses the unified chain including ALOOP.md."""
        _make_instruction_file(tmp_path, "ALOOP.md", "# From ALOOP")
        content = _find_agents_md(tmp_path)
        assert "From ALOOP" in content

    def test_find_agents_md_fallback_to_claude(self, tmp_path):
        _make_instruction_file(tmp_path, ".claude/CLAUDE.md", "# CLAUDE content")
        content = _find_agents_md(tmp_path)
        assert "CLAUDE content" in content

    def test_find_agents_md_empty_when_none(self, tmp_path):
        assert _find_agents_md(tmp_path) == ""

    def test_template_mode_uses_unified_chain(self, tmp_path):
        """Template mode {{agents_md}} now uses the unified discovery chain."""
        # Set up template config
        _make_config(tmp_path, {"system_prompt": "file:PROMPT.md"})
        (tmp_path / "PROMPT.md").write_text("{{agents_md}}")
        _make_instruction_file(tmp_path, "ALOOP.md", "---\ntitle: test\n---\n# ALOOP body")

        prompt = build_system_prompt(project_root=tmp_path)
        assert "ALOOP body" in prompt

    def test_template_mode_falls_through_to_agents(self, tmp_path):
        _make_config(tmp_path, {"system_prompt": "file:PROMPT.md"})
        (tmp_path / "PROMPT.md").write_text("Context: {{agents_md}}")
        _make_instruction_file(tmp_path, "AGENTS.md", "# Project AGENTS")

        prompt = build_system_prompt(project_root=tmp_path)
        assert "Project AGENTS" in prompt

    def test_section_mode_uses_unified_chain(self, tmp_path):
        """Section mode also gets ALOOP.md via the unified chain."""
        _make_instruction_file(tmp_path, "ALOOP.md", "# My ALOOP project")
        prompt = build_system_prompt(project_root=tmp_path)
        assert "My ALOOP project" in prompt


# ---------------------------------------------------------------------------
# Skills discovery (merged)
# ---------------------------------------------------------------------------


class TestSkillsDiscovery:
    """Test skill merging across .aloop/skills/, .agents/skills/, .claude/skills/."""

    def test_aloop_skills_dir_first(self, tmp_path):
        (tmp_path / ".aloop" / "skills").mkdir(parents=True)
        (tmp_path / ".agents" / "skills").mkdir(parents=True)
        dirs = _find_skills_dirs(tmp_path)
        assert dirs[0] == tmp_path / ".aloop" / "skills"
        assert dirs[1] == tmp_path / ".agents" / "skills"

    def test_all_three_dirs(self, tmp_path):
        for d in [".aloop/skills", ".agents/skills", ".claude/skills"]:
            (tmp_path / d).mkdir(parents=True)
        dirs = _find_skills_dirs(tmp_path)
        assert len(dirs) == 3

    def test_only_claude_skills(self, tmp_path):
        (tmp_path / ".claude" / "skills").mkdir(parents=True)
        dirs = _find_skills_dirs(tmp_path)
        assert len(dirs) == 1
        assert dirs[0] == tmp_path / ".claude" / "skills"

    def test_no_skills_dirs(self, tmp_path):
        assert _find_skills_dirs(tmp_path) == []

    def test_find_skills_dir_returns_highest_priority(self, tmp_path):
        (tmp_path / ".aloop" / "skills").mkdir(parents=True)
        (tmp_path / ".claude" / "skills").mkdir(parents=True)
        assert _find_skills_dir(tmp_path) == tmp_path / ".aloop" / "skills"

    def test_skills_merged_in_system_prompt(self, tmp_path):
        """Skills from multiple dirs appear in system prompt."""
        aloop_skills = tmp_path / ".aloop" / "skills"
        claude_skills = tmp_path / ".claude" / "skills"
        _make_skill(aloop_skills, "alpha", "name: alpha\ndescription: from aloop\n")
        _make_skill(claude_skills, "beta", "name: beta\ndescription: from claude\n")

        section = _build_skill_section(tmp_path)
        assert "alpha: from aloop" in section
        assert "beta: from claude" in section

    def test_skills_project_overrides_on_collision(self, tmp_path):
        """Higher-priority dir wins on name collision."""
        aloop_skills = tmp_path / ".aloop" / "skills"
        claude_skills = tmp_path / ".claude" / "skills"
        _make_skill(aloop_skills, "deploy", "name: deploy\ndescription: aloop deploy\n")
        _make_skill(claude_skills, "deploy", "name: deploy\ndescription: claude deploy\n")

        section = _build_skill_section(tmp_path)
        assert "aloop deploy" in section
        assert "claude deploy" not in section

    def test_global_skills_dir_included(self, tmp_path, monkeypatch):
        """~/.aloop/skills/ is included in skill discovery."""
        fake_home = tmp_path / "home"
        global_skills = fake_home / ".aloop" / "skills"
        global_skills.mkdir(parents=True)
        _make_skill(global_skills, "global-tool", "name: global-tool\ndescription: from global\n")

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.setenv("ALOOP_PROJECT_ROOT", str(tmp_path / "project"))
        (tmp_path / "project").mkdir()

        dirs = _find_all_skills_dirs()
        assert global_skills in dirs

    def test_project_skills_override_global(self, tmp_path, monkeypatch):
        """Project skills override global on name collision."""
        fake_home = tmp_path / "home"
        global_skills = fake_home / ".aloop" / "skills"
        project_skills = tmp_path / "project" / ".aloop" / "skills"

        _make_skill(global_skills, "deploy", "name: deploy\ndescription: global version\n")
        _make_skill(project_skills, "deploy", "name: deploy\ndescription: project version\n")

        result = _discover_skills(
            skills_dirs=[project_skills, global_skills],
            disabled_skills=set(),
        )
        assert result["deploy"]["description"] == "project version"


# ---------------------------------------------------------------------------
# Deep merge
# ---------------------------------------------------------------------------


class TestDeepMerge:
    def test_flat_merge(self):
        assert _deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_override_wins(self):
        assert _deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_nested_merge(self):
        base = {"x": {"a": 1, "b": 2}}
        over = {"x": {"b": 3, "c": 4}}
        assert _deep_merge(base, over) == {"x": {"a": 1, "b": 3, "c": 4}}

    def test_override_replaces_non_dict(self):
        assert _deep_merge({"a": "string"}, {"a": {"nested": True}}) == {"a": {"nested": True}}

    def test_empty_base(self):
        assert _deep_merge({}, {"a": 1}) == {"a": 1}

    def test_empty_override(self):
        assert _deep_merge({"a": 1}, {}) == {"a": 1}

    def test_deeply_nested(self):
        base = {"a": {"b": {"c": 1, "d": 2}}}
        over = {"a": {"b": {"d": 3, "e": 4}}}
        assert _deep_merge(base, over) == {"a": {"b": {"c": 1, "d": 3, "e": 4}}}


# ---------------------------------------------------------------------------
# Global + project config merging
# ---------------------------------------------------------------------------


class TestConfigMerging:
    def test_project_only(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        _make_config(tmp_path, {"provider": "openai", "model": "gpt-4o"})
        config = _load_aloop_config(tmp_path)
        assert config["provider"] == "openai"
        assert config["model"] == "gpt-4o"

    def test_global_only(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        _make_config(fake_home, {"provider": "anthropic"}, global_config=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        config = _load_aloop_config(tmp_path)
        assert config["provider"] == "anthropic"

    def test_project_overrides_global(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        _make_config(fake_home, {"provider": "anthropic", "model": "claude"}, global_config=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        _make_config(tmp_path, {"provider": "openai"})
        config = _load_aloop_config(tmp_path)
        assert config["provider"] == "openai"
        assert config["model"] == "claude"  # inherited from global

    def test_deep_merge_sections(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        _make_config(fake_home, {
            "sections": {"preamble": False, "identity": "global identity"}
        }, global_config=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        _make_config(tmp_path, {
            "sections": {"identity": "project identity"}
        })
        config = _load_aloop_config(tmp_path)
        assert config["sections"]["preamble"] is False  # from global
        assert config["sections"]["identity"] == "project identity"  # overridden

    def test_no_config_files(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        config = _load_aloop_config(tmp_path)
        assert config == {}

    def test_invalid_global_json(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        p = fake_home / ".aloop" / "config.json"
        p.parent.mkdir(parents=True)
        p.write_text("not json{{{")
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        _make_config(tmp_path, {"provider": "openai"})
        config = _load_aloop_config(tmp_path)
        assert config["provider"] == "openai"  # project still works


# ---------------------------------------------------------------------------
# disabled_hooks / disabled_skills
# ---------------------------------------------------------------------------


class TestDisabledHooksSkills:
    def test_disabled_skills_in_config(self, tmp_path, monkeypatch):
        """Skills listed in disabled_skills are excluded from discovery."""
        sd = tmp_path / "skills"
        sd.mkdir()
        _make_skill(sd, "alpha", "name: alpha\ndescription: alpha\n")
        _make_skill(sd, "beta", "name: beta\ndescription: beta\n")
        _make_skill(sd, "gamma", "name: gamma\ndescription: gamma\n")

        result = _discover_skills(
            skills_dirs=[sd],
            disabled_skills={"alpha", "gamma"},
        )
        assert "alpha" not in result
        assert "gamma" not in result
        assert "beta" in result

    def test_disabled_skills_empty_list(self, tmp_path):
        sd = tmp_path / "skills"
        sd.mkdir()
        _make_skill(sd, "alpha", "name: alpha\ndescription: alpha\n")

        result = _discover_skills(skills_dirs=[sd], disabled_skills=set())
        assert "alpha" in result

    def test_disabled_hooks_loaded_from_config(self, tmp_path, monkeypatch):
        """disabled_hooks key is present in merged config."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        _make_config(tmp_path, {"disabled_hooks": ["safety", "logging"]})
        config = _load_aloop_config(tmp_path)
        assert config["disabled_hooks"] == ["safety", "logging"]

    def test_disabled_skills_loaded_from_config(self, tmp_path, monkeypatch):
        """disabled_skills key is present in merged config."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        _make_config(tmp_path, {"disabled_skills": ["deploy"]})
        config = _load_aloop_config(tmp_path)
        assert config["disabled_skills"] == ["deploy"]


# ---------------------------------------------------------------------------
# Hook discovery (global + project)
# ---------------------------------------------------------------------------


class TestHookDiscovery:
    def test_project_hooks_detected(self, tmp_path):
        hooks_dir = tmp_path / ".aloop" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "__init__.py").write_text("")
        (hooks_dir / "safety.py").write_text("# safety hook")
        (hooks_dir / "logging.py").write_text("# logging hook")
        (hooks_dir / "_private.py").write_text("# private, skipped")

        info = get_discovered_hooks(tmp_path)
        assert "safety" in info["project"]
        assert "logging" in info["project"]
        assert "_private" not in info["project"]

    def test_global_hooks_detected(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        global_hooks = fake_home / ".aloop" / "hooks"
        global_hooks.mkdir(parents=True)
        (global_hooks / "audit.py").write_text("# audit hook")

        monkeypatch.setattr(Path, "home", lambda: fake_home)

        info = get_discovered_hooks(tmp_path)
        assert "audit" in info["global"]

    def test_both_scopes(self, tmp_path, monkeypatch):
        # Global
        fake_home = tmp_path / "home"
        global_hooks = fake_home / ".aloop" / "hooks"
        global_hooks.mkdir(parents=True)
        (global_hooks / "global_hook.py").write_text("")
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        # Project
        project_hooks = tmp_path / ".aloop" / "hooks"
        project_hooks.mkdir(parents=True)
        (project_hooks / "project_hook.py").write_text("")

        info = get_discovered_hooks(tmp_path)
        assert "global_hook" in info["global"]
        assert "project_hook" in info["project"]

    def test_no_hooks(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        info = get_discovered_hooks(tmp_path)
        assert info == {"global": [], "project": []}


# ---------------------------------------------------------------------------
# Integration: build_system_prompt with new features
# ---------------------------------------------------------------------------


class TestSystemPromptIntegration:
    def test_section_mode_with_aloop_md(self, tmp_path):
        """Section mode picks up ALOOP.md via unified chain."""
        _make_instruction_file(tmp_path, "ALOOP.md", "# ALOOP instructions")
        prompt = build_system_prompt(project_root=tmp_path)
        assert "ALOOP instructions" in prompt
        assert "Project Context" in prompt

    def test_section_mode_with_agents_md(self, tmp_path):
        """Section mode still works with AGENTS.md."""
        _make_instruction_file(tmp_path, "AGENTS.md", "# AGENTS instructions")
        prompt = build_system_prompt(project_root=tmp_path)
        assert "AGENTS instructions" in prompt

    def test_section_mode_stripped_frontmatter(self, tmp_path):
        _make_instruction_file(tmp_path, "ALOOP.md", "---\ntitle: test\n---\n# Body only")
        prompt = build_system_prompt(project_root=tmp_path)
        assert "Body only" in prompt
        assert "title: test" not in prompt

    def test_merged_config_affects_prompt(self, tmp_path, monkeypatch):
        """Global config sections are used when project doesn't override."""
        fake_home = tmp_path / "home"
        _make_config(fake_home, {
            "sections": {"preamble": "Global preamble"}
        }, global_config=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        prompt = build_system_prompt(project_root=tmp_path)
        assert "Global preamble" in prompt

    def test_project_config_overrides_global_in_prompt(self, tmp_path, monkeypatch):
        """Project section overrides global."""
        fake_home = tmp_path / "home"
        _make_config(fake_home, {
            "sections": {"preamble": "Global preamble"}
        }, global_config=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        _make_config(tmp_path, {
            "sections": {"preamble": "Project preamble"}
        })

        prompt = build_system_prompt(project_root=tmp_path)
        assert "Project preamble" in prompt
        assert "Global preamble" not in prompt

    def test_merged_skills_in_prompt(self, tmp_path):
        """Skills from multiple dirs appear in the system prompt."""
        _make_skill(tmp_path / ".aloop" / "skills", "s1", "name: s1\ndescription: d1\n")
        _make_skill(tmp_path / ".claude" / "skills", "s2", "name: s2\ndescription: d2\n")

        prompt = build_system_prompt(project_root=tmp_path)
        assert "s1: d1" in prompt
        assert "s2: d2" in prompt
