"""Tests for aloop skill bridge (tools/skills.py)."""

import pytest
from pathlib import Path

from aloop.tools.skills import (
    _parse_frontmatter,
    _discover_skills,
    _get_skills,
    build_skill_listing,
    list_skill_names,
    _load_skill,
    get_skills_by_source,
)


# --- Helpers ---


def _make_skill(skills_dir: Path, name: str, frontmatter: str = "", body: str = ""):
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}---\n{body}")


@pytest.fixture
def skills_dir(tmp_path, monkeypatch):
    """Create a single skills directory and wire it up."""
    sd = tmp_path / "skills"
    sd.mkdir()
    monkeypatch.setattr("aloop.tools.skills._find_all_skills_dirs", lambda: [sd])
    monkeypatch.setattr("aloop.tools.skills._load_disabled_skills", lambda: set())
    monkeypatch.setattr("aloop.tools.skills._skill_cache", None)
    return sd


# --- _parse_frontmatter ---


def test_parse_frontmatter_valid():
    text = "---\nname: foo\ndescription: bar\n---\n# Body"
    fm = _parse_frontmatter(text)
    assert fm == {"name": "foo", "description": "bar"}


def test_parse_frontmatter_quoted_values():
    text = '---\nname: "quoted"\ntag: \'single\'\n---\n'
    fm = _parse_frontmatter(text)
    assert fm["name"] == "quoted"
    assert fm["tag"] == "single"


def test_parse_frontmatter_no_frontmatter():
    assert _parse_frontmatter("# Just markdown") == {}


def test_parse_frontmatter_unclosed():
    assert _parse_frontmatter("---\nname: foo\nno closing") == {}


def test_parse_frontmatter_empty_body():
    text = "---\nname: x\n---\n"
    fm = _parse_frontmatter(text)
    assert fm == {"name": "x"}


def test_parse_frontmatter_colon_in_value():
    text = "---\nurl: http://example.com:8080/path\n---\n"
    fm = _parse_frontmatter(text)
    assert fm["url"] == "http://example.com:8080/path"


# --- _discover_skills ---


def test_discover_skills_valid(skills_dir):
    _make_skill(skills_dir, "alpha", "name: Alpha Skill\ndescription: does alpha\n", "body")
    _make_skill(skills_dir, "beta", "description: does beta\n", "body")
    # dir without SKILL.md — should be skipped
    (skills_dir / "empty-dir").mkdir()

    result = _discover_skills(skills_dirs=[skills_dir])
    assert "Alpha Skill" in result  # uses frontmatter name
    assert "beta" in result  # falls back to dir name
    assert "empty-dir" not in result
    assert len(result) == 2


def test_discover_skills_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("aloop.tools.skills._find_all_skills_dirs", lambda: [tmp_path / "nope"])
    monkeypatch.setattr("aloop.tools.skills._skill_cache", None)
    assert _discover_skills(skills_dirs=[tmp_path / "nope"]) == {}


def test_discover_skills_read_error(skills_dir, monkeypatch):
    d = skills_dir / "broken"
    d.mkdir()
    skill_md = d / "SKILL.md"
    skill_md.write_text("---\nname: broken\n---\n")
    # Monkeypatch read_text to raise OSError
    orig_read_text = Path.read_text

    def bad_read(self, *a, **kw):
        if "broken" in str(self):
            raise OSError("permission denied")
        return orig_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", bad_read)

    # Add a good skill too
    _make_skill(skills_dir, "good", "name: good\n", "body")
    result = _discover_skills(skills_dirs=[skills_dir])
    assert "good" in result
    assert "broken" not in result


# --- build_skill_listing ---


def test_build_skill_listing_basic(skills_dir):
    _make_skill(skills_dir, "a-skill", "name: a-skill\ndescription: first\n")
    _make_skill(skills_dir, "b-skill", "name: b-skill\ndescription: second\n")
    listing = build_skill_listing()
    assert "The following skills are available via the load_skill tool" in listing
    assert "- a-skill: first" in listing
    assert "- b-skill: second" in listing


def test_build_skill_listing_empty(skills_dir):
    assert build_skill_listing() == ""


def test_build_skill_listing_budget_enforcement(skills_dir):
    _make_skill(skills_dir, "aaa", "name: aaa\ndescription: short\n")
    _make_skill(skills_dir, "bbb", "name: bbb\ndescription: also short\n")
    # Budget so tight only first skill line fits
    listing = build_skill_listing(max_chars=20)
    assert "aaa" in listing
    assert "bbb" not in listing


def test_build_skill_listing_desc_truncation(skills_dir):
    long_desc = "x" * 300
    _make_skill(skills_dir, "long", f"name: long\ndescription: {long_desc}\n")
    listing = build_skill_listing()
    assert "..." in listing
    # The truncated description should be at most 250 chars
    line = [l for l in listing.splitlines() if l.startswith("- long:")][0]
    desc_part = line.split(": ", 1)[1]
    assert len(desc_part) <= 250


# --- list_skill_names ---


def test_list_skill_names_sorted(skills_dir):
    _make_skill(skills_dir, "z-skill", "name: z-skill\n")
    _make_skill(skills_dir, "a-skill", "name: a-skill\n")
    _make_skill(skills_dir, "m-skill", "name: m-skill\n")
    names = list_skill_names()
    assert names == ["a-skill", "m-skill", "z-skill"]


# --- _load_skill ---


@pytest.mark.asyncio
async def test_load_skill_valid(skills_dir):
    _make_skill(skills_dir, "checkin", "name: checkin\n", "# Checkin instructions")
    _get_skills()  # populate cache
    result = await _load_skill("checkin")
    assert result.is_error is False
    assert "Checkin instructions" in result.content


@pytest.mark.asyncio
async def test_load_skill_invalid(skills_dir):
    _make_skill(skills_dir, "real", "name: real\n")
    _get_skills()
    result = await _load_skill("nonexistent")
    assert result.is_error is True
    assert "Unknown skill" in result.content
    assert "real" in result.content  # lists available skills


@pytest.mark.asyncio
async def test_load_skill_variable_substitution(skills_dir):
    body = "Run $ARGUMENTS and ${ARGUMENTS} in ${CLAUDE_SKILL_DIR}/scripts"
    _make_skill(skills_dir, "sub", "name: sub\n", body)
    _get_skills()
    result = await _load_skill("sub", args="my-arg")
    assert "my-arg" in result.content
    assert "$ARGUMENTS" not in result.content
    assert "${ARGUMENTS}" not in result.content
    assert "${CLAUDE_SKILL_DIR}" not in result.content
    # CLAUDE_SKILL_DIR should be replaced with the actual dir path
    assert str(skills_dir / "sub") in result.content


@pytest.mark.asyncio
async def test_load_skill_read_error(skills_dir, monkeypatch):
    _make_skill(skills_dir, "broke", "name: broke\n", "content")
    _get_skills()  # cache it
    # Now delete the file so read fails
    (skills_dir / "broke" / "SKILL.md").unlink()
    result = await _load_skill("broke")
    assert result.is_error is True
    assert "Error reading skill" in result.content


# --- Merged skill discovery ---


def test_discover_skills_merged_across_dirs(tmp_path, monkeypatch):
    """Skills from multiple directories are merged."""
    dir1 = tmp_path / "project_skills"
    dir2 = tmp_path / "global_skills"
    dir1.mkdir()
    dir2.mkdir()

    _make_skill(dir1, "alpha", "name: alpha\ndescription: project alpha\n")
    _make_skill(dir2, "beta", "name: beta\ndescription: global beta\n")

    monkeypatch.setattr("aloop.tools.skills._skill_cache", None)
    result = _discover_skills(skills_dirs=[dir1, dir2], disabled_skills=set())
    assert "alpha" in result
    assert "beta" in result
    assert len(result) == 2


def test_discover_skills_project_overrides_global(tmp_path, monkeypatch):
    """When same skill name exists in project and global, project wins."""
    project = tmp_path / "project_skills"
    global_d = tmp_path / "global_skills"
    project.mkdir()
    global_d.mkdir()

    _make_skill(project, "deploy", "name: deploy\ndescription: project deploy\n")
    _make_skill(global_d, "deploy", "name: deploy\ndescription: global deploy\n")

    monkeypatch.setattr("aloop.tools.skills._skill_cache", None)
    # project first in priority order
    result = _discover_skills(skills_dirs=[project, global_d], disabled_skills=set())
    assert result["deploy"]["description"] == "project deploy"
    assert str(project) in result["deploy"]["path"]


def test_discover_skills_disabled_skills(tmp_path, monkeypatch):
    """Disabled skills are excluded from discovery."""
    sd = tmp_path / "skills"
    sd.mkdir()

    _make_skill(sd, "alpha", "name: alpha\ndescription: alpha\n")
    _make_skill(sd, "beta", "name: beta\ndescription: beta\n")

    monkeypatch.setattr("aloop.tools.skills._skill_cache", None)
    result = _discover_skills(skills_dirs=[sd], disabled_skills={"alpha"})
    assert "alpha" not in result
    assert "beta" in result


def test_get_skills_by_source(tmp_path, monkeypatch):
    """get_skills_by_source returns skills grouped by directory."""
    dir1 = tmp_path / "project_skills"
    dir2 = tmp_path / "global_skills"
    dir1.mkdir()
    dir2.mkdir()

    _make_skill(dir1, "alpha", "name: alpha\ndescription: pa\n")
    _make_skill(dir2, "beta", "name: beta\ndescription: gb\n")

    monkeypatch.setattr("aloop.tools.skills._find_all_skills_dirs", lambda: [dir1, dir2])
    monkeypatch.setattr("aloop.tools.skills._load_disabled_skills", lambda: set())
    monkeypatch.setattr("aloop.tools.skills._skill_cache", None)

    by_source = get_skills_by_source()
    assert str(dir1) in by_source
    assert str(dir2) in by_source
    assert "alpha" in by_source[str(dir1)]
    assert "beta" in by_source[str(dir2)]
