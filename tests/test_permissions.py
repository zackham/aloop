"""Tests for the permission system and read-only exploration tools."""

import os
import asyncio
import pytest_asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from aloop.permissions import (
    PermissionDenied,
    check_permissions,
    HARDCODED_DENY_WRITE,
)
from aloop.tools.search import (
    grep_tool,
    find_tool,
    ls_tool,
    _truncate_output,
    _truncate_line,
    _resolve_search_path,
)
from aloop.tools import CODING_TOOLS, READONLY_TOOLS, ALL_TOOLS, ANALYSIS_TOOLS


# ── Tool set definitions ──


class TestToolSets:
    def test_coding_tools_has_bash(self):
        names = {t.name for t in CODING_TOOLS}
        assert "bash" in names
        assert "read_file" in names
        assert "write_file" in names
        assert "edit_file" in names
        assert "load_skill" in names

    def test_coding_tools_no_readonly(self):
        names = {t.name for t in CODING_TOOLS}
        assert "grep" not in names
        assert "find" not in names
        assert "ls" not in names

    def test_readonly_tools_no_bash(self):
        names = {t.name for t in READONLY_TOOLS}
        assert "bash" not in names
        assert "write_file" not in names
        assert "edit_file" not in names

    def test_readonly_tools_has_exploration(self):
        names = {t.name for t in READONLY_TOOLS}
        assert "read_file" in names
        assert "grep" in names
        assert "find" in names
        assert "ls" in names
        assert "load_skill" in names

    def test_all_tools_is_union(self):
        all_names = {t.name for t in ALL_TOOLS}
        coding_names = {t.name for t in CODING_TOOLS}
        readonly_names = {t.name for t in READONLY_TOOLS}
        assert all_names == coding_names | readonly_names

    def test_analysis_tools_is_coding_tools(self):
        """ANALYSIS_TOOLS is backward compat alias for CODING_TOOLS."""
        assert ANALYSIS_TOOLS is CODING_TOOLS


# ── Permission checks ──


class TestPermissionChecks:
    def test_no_permissions_allows_everything(self):
        """No permissions config = yolo."""
        check_permissions("bash", {"command": "rm -rf /tmp/foo"})
        check_permissions("write_file", {"path": "/etc/passwd"})
        check_permissions("read_file", {"path": ".env"})

    def test_hardcoded_bash_deny(self):
        """Hardcoded bash denies are always active, even without permissions config."""
        with pytest.raises(PermissionDenied, match="safety"):
            check_permissions("bash", {"command": "rm -rf /"})
        with pytest.raises(PermissionDenied, match="safety"):
            check_permissions("bash", {"command": "rm -rf ~"})
        with pytest.raises(PermissionDenied, match="safety"):
            check_permissions("bash", {"command": ":(){ :|:& };:"})

    def test_hardcoded_bash_deny_allows_safe_rm(self):
        """rm -rf on a specific path is fine."""
        check_permissions("bash", {"command": "rm -rf /tmp/build"})

    def test_tool_set_enforcement(self):
        allowed = {"read_file", "grep", "find", "ls", "load_skill"}
        check_permissions("read_file", {"path": "foo.py"}, allowed_tools=allowed)
        check_permissions("grep", {"pattern": "test"}, allowed_tools=allowed)
        with pytest.raises(PermissionDenied, match="not available"):
            check_permissions("bash", {"command": "ls"}, allowed_tools=allowed)
        with pytest.raises(PermissionDenied, match="not available"):
            check_permissions("write_file", {"path": "f.py", "content": ""}, allowed_tools=allowed)

    def test_path_deny_blocks_read(self, tmp_path):
        perms = {"paths": {"deny": [".env", "**/*.key"]}}
        with pytest.raises(PermissionDenied, match="denied"):
            check_permissions("read_file", {"path": ".env"}, permissions=perms, project_root=tmp_path)
        with pytest.raises(PermissionDenied, match="denied"):
            check_permissions("read_file", {"path": "secrets/api.key"}, permissions=perms, project_root=tmp_path)

    def test_path_deny_blocks_write(self, tmp_path):
        perms = {"paths": {"deny": [".env"]}}
        with pytest.raises(PermissionDenied, match="denied"):
            check_permissions("write_file", {"path": ".env", "content": "x"}, permissions=perms, project_root=tmp_path)

    def test_path_deny_blocks_grep(self, tmp_path):
        """Deny patterns should block grep on denied paths."""
        perms = {"paths": {"deny": ["secrets/**"]}}
        with pytest.raises(PermissionDenied, match="denied"):
            check_permissions("grep", {"pattern": "key", "path": "secrets/keys"}, permissions=perms, project_root=tmp_path)

    def test_path_deny_allows_normal(self, tmp_path):
        perms = {"paths": {"deny": [".env"]}}
        check_permissions("read_file", {"path": "src/main.py"}, permissions=perms, project_root=tmp_path)

    def test_project_containment(self, tmp_path):
        perms = {"paths": {"allow_outside_project": False}}
        # Inside project: ok
        check_permissions("read_file", {"path": "src/main.py"}, permissions=perms, project_root=tmp_path)
        # Outside project: blocked
        with pytest.raises(PermissionDenied, match="outside project"):
            check_permissions("read_file", {"path": "/etc/passwd"}, permissions=perms, project_root=tmp_path)

    def test_project_containment_additional_dirs(self, tmp_path):
        other = tmp_path / "other-project"
        other.mkdir()
        perms = {
            "paths": {
                "allow_outside_project": False,
                "additional_dirs": [str(other)],
            }
        }
        # Additional dir: ok
        check_permissions("read_file", {"path": str(other / "file.py")}, permissions=perms, project_root=tmp_path)

    def test_write_path_restriction(self, tmp_path):
        perms = {"paths": {"write": ["src/**", "tests/**"]}}
        # Allowed write path
        check_permissions("write_file", {"path": "src/main.py", "content": "x"}, permissions=perms, project_root=tmp_path)
        # Denied write path
        with pytest.raises(PermissionDenied, match="not in allowed write"):
            check_permissions("write_file", {"path": "docs/readme.md", "content": "x"}, permissions=perms, project_root=tmp_path)

    def test_write_restriction_doesnt_affect_read(self, tmp_path):
        perms = {"paths": {"write": ["src/**"]}}
        # Reading docs is fine even though writes are restricted to src
        check_permissions("read_file", {"path": "docs/readme.md"}, permissions=perms, project_root=tmp_path)

    def test_hardcoded_write_deny(self, tmp_path):
        perms = {"paths": {}}
        with pytest.raises(PermissionDenied, match="safety"):
            check_permissions("write_file", {"path": ".git/config", "content": "x"}, permissions=perms, project_root=tmp_path)
        with pytest.raises(PermissionDenied, match="safety"):
            check_permissions("edit_file", {"path": ".aloop/config.json", "old_string": "a", "new_string": "b"}, permissions=perms, project_root=tmp_path)

    def test_no_path_arg_allowed(self):
        """grep/find with no path arg (defaults to '.') should be allowed."""
        perms = {"paths": {"deny": ["secrets/**"]}}
        check_permissions("grep", {"pattern": "test"}, permissions=perms)
        check_permissions("find", {"pattern": "*.py"}, permissions=perms)

    def test_permission_denied_is_tool_rejected_subclass(self):
        from aloop.tools_base import ToolRejected
        assert issubclass(PermissionDenied, ToolRejected)


# ── Truncation utilities ──


class TestTruncation:
    def test_truncate_output_under_limit(self):
        text = "hello\nworld"
        result, truncated = _truncate_output(text, max_bytes=100)
        assert result == text
        assert not truncated

    def test_truncate_output_over_limit(self):
        text = "line1\nline2\nline3\nline4"
        result, truncated = _truncate_output(text, max_bytes=12)
        assert truncated
        assert "line1" in result
        assert "\n" in result or len(result) <= 12

    def test_truncate_line_short(self):
        assert _truncate_line("short") == "short"

    def test_truncate_line_long(self):
        long_line = "x" * 600
        result = _truncate_line(long_line)
        assert len(result) < 600
        assert result.endswith("... [truncated]")


# ── Search tool schemas ──


class TestToolSchemas:
    def test_grep_tool_schema(self):
        schema = grep_tool.to_schema()
        assert schema["function"]["name"] == "grep"
        params = schema["function"]["parameters"]
        assert "pattern" in params["properties"]
        assert "pattern" in params["required"]

    def test_find_tool_schema(self):
        schema = find_tool.to_schema()
        assert schema["function"]["name"] == "find"
        params = schema["function"]["parameters"]
        assert "pattern" in params["properties"]

    def test_ls_tool_schema(self):
        schema = ls_tool.to_schema()
        assert schema["function"]["name"] == "ls"
        params = schema["function"]["parameters"]
        assert "path" in params["properties"]


# ── ls tool execution ──


class TestLsTool:
    @pytest.mark.asyncio
    async def test_ls_project_root(self, tmp_path):
        (tmp_path / "file.txt").write_text("hello")
        (tmp_path / "subdir").mkdir()
        (tmp_path / ".hidden").write_text("dot")

        with patch("aloop.tools.search.get_project_root", return_value=tmp_path):
            result = await ls_tool.execute()
            assert "file.txt" in result.content
            assert "subdir/" in result.content
            assert ".hidden" in result.content
            assert not result.is_error

    @pytest.mark.asyncio
    async def test_ls_specific_dir(self, tmp_path):
        subdir = tmp_path / "src"
        subdir.mkdir()
        (subdir / "main.py").write_text("code")

        with patch("aloop.tools.search.get_project_root", return_value=tmp_path):
            result = await ls_tool.execute(path="src")
            assert "main.py" in result.content

    @pytest.mark.asyncio
    async def test_ls_nonexistent(self, tmp_path):
        with patch("aloop.tools.search.get_project_root", return_value=tmp_path):
            result = await ls_tool.execute(path="nope")
            assert result.is_error
            assert "not found" in result.content.lower()

    @pytest.mark.asyncio
    async def test_ls_not_a_directory(self, tmp_path):
        (tmp_path / "file.txt").write_text("hello")
        with patch("aloop.tools.search.get_project_root", return_value=tmp_path):
            result = await ls_tool.execute(path="file.txt")
            assert result.is_error
            assert "not a directory" in result.content.lower()

    @pytest.mark.asyncio
    async def test_ls_empty_dir(self, tmp_path):
        (tmp_path / "empty").mkdir()
        with patch("aloop.tools.search.get_project_root", return_value=tmp_path):
            result = await ls_tool.execute(path="empty")
            assert "empty directory" in result.content.lower()

    @pytest.mark.asyncio
    async def test_ls_limit(self, tmp_path):
        for i in range(10):
            (tmp_path / f"file{i:02d}.txt").write_text("")
        with patch("aloop.tools.search.get_project_root", return_value=tmp_path):
            result = await ls_tool.execute(limit=3)
            lines = [l for l in result.content.split("\n") if l.strip() and not l.startswith("[")]
            assert len(lines) == 3
            assert "limit reached" in result.content

    @pytest.mark.asyncio
    async def test_ls_sorted_case_insensitive(self, tmp_path):
        (tmp_path / "Banana").write_text("")
        (tmp_path / "apple").write_text("")
        (tmp_path / "Cherry").write_text("")
        with patch("aloop.tools.search.get_project_root", return_value=tmp_path):
            result = await ls_tool.execute()
            lines = result.content.strip().split("\n")
            assert lines[0] == "apple"
            assert lines[1] == "Banana"
            assert lines[2] == "Cherry"


# ── find tool execution ──


class TestFindTool:
    @pytest.mark.asyncio
    async def test_find_with_python_glob(self, tmp_path):
        """Test find fallback when fd is not available."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("code")
        (tmp_path / "src" / "test.py").write_text("test")
        (tmp_path / "readme.md").write_text("docs")

        with patch("aloop.tools.search.get_project_root", return_value=tmp_path), \
             patch("shutil.which", return_value=None):
            result = await find_tool.execute(pattern="**/*.py")
            assert "main.py" in result.content
            assert "test.py" in result.content
            assert "readme.md" not in result.content
            assert not result.is_error

    @pytest.mark.asyncio
    async def test_find_nonexistent_path(self, tmp_path):
        with patch("aloop.tools.search.get_project_root", return_value=tmp_path):
            result = await find_tool.execute(pattern="*.py", path="nope")
            assert result.is_error

    @pytest.mark.asyncio
    async def test_find_no_matches(self, tmp_path):
        with patch("aloop.tools.search.get_project_root", return_value=tmp_path), \
             patch("shutil.which", return_value=None):
            result = await find_tool.execute(pattern="*.xyz")
            assert "no files found" in result.content.lower()

    @pytest.mark.asyncio
    async def test_find_filters_noise_dirs(self, tmp_path):
        """Python glob fallback should skip node_modules, .git, etc."""
        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("x")
        (tmp_path / "src").mkdir(parents=True)
        (tmp_path / "src" / "app.js").write_text("x")

        with patch("aloop.tools.search.get_project_root", return_value=tmp_path), \
             patch("shutil.which", return_value=None):
            result = await find_tool.execute(pattern="**/*.js")
            assert "app.js" in result.content
            assert "node_modules" not in result.content


# ── grep tool execution ──


class TestGrepTool:
    @pytest.mark.asyncio
    async def test_grep_no_rg(self, tmp_path):
        """When rg is not installed, return helpful error."""
        with patch("shutil.which", return_value=None), \
             patch("aloop.tools.search.get_project_root", return_value=tmp_path):
            result = await grep_tool.execute(pattern="test")
            assert result.is_error
            assert "ripgrep" in result.content.lower()

    @pytest.mark.asyncio
    async def test_grep_nonexistent_path(self, tmp_path):
        with patch("aloop.tools.search.get_project_root", return_value=tmp_path):
            result = await grep_tool.execute(pattern="test", path="nope")
            assert result.is_error

    @pytest.mark.asyncio
    @pytest.mark.skipif(not os.path.exists("/usr/bin/rg") and not os.path.exists("/usr/local/bin/rg"),
                        reason="ripgrep not installed")
    async def test_grep_real_search(self, tmp_path):
        """Integration test with real ripgrep."""
        (tmp_path / "test.py").write_text("def hello_world():\n    pass\n")
        (tmp_path / "other.py").write_text("# nothing here\n")

        with patch("aloop.tools.search.get_project_root", return_value=tmp_path):
            result = await grep_tool.execute(pattern="hello_world")
            assert "hello_world" in result.content
            assert "test.py" in result.content
            assert not result.is_error

    @pytest.mark.asyncio
    @pytest.mark.skipif(not os.path.exists("/usr/bin/rg") and not os.path.exists("/usr/local/bin/rg"),
                        reason="ripgrep not installed")
    async def test_grep_no_matches(self, tmp_path):
        (tmp_path / "test.py").write_text("nothing interesting\n")
        with patch("aloop.tools.search.get_project_root", return_value=tmp_path):
            result = await grep_tool.execute(pattern="zzz_not_found_zzz")
            assert "no matches" in result.content.lower()

    @pytest.mark.asyncio
    @pytest.mark.skipif(not os.path.exists("/usr/bin/rg") and not os.path.exists("/usr/local/bin/rg"),
                        reason="ripgrep not installed")
    async def test_grep_with_glob_filter(self, tmp_path):
        (tmp_path / "test.py").write_text("hello\n")
        (tmp_path / "test.js").write_text("hello\n")
        with patch("aloop.tools.search.get_project_root", return_value=tmp_path):
            result = await grep_tool.execute(pattern="hello", glob="*.py")
            assert "test.py" in result.content
            assert "test.js" not in result.content


# ── Path resolution ──


class TestPathResolution:
    def test_resolve_default(self, tmp_path):
        with patch("aloop.tools.search.get_project_root", return_value=tmp_path):
            assert _resolve_search_path(None) == tmp_path
            assert _resolve_search_path(".") == tmp_path

    def test_resolve_relative(self, tmp_path):
        (tmp_path / "src").mkdir()
        with patch("aloop.tools.search.get_project_root", return_value=tmp_path):
            assert _resolve_search_path("src") == tmp_path / "src"

    def test_resolve_absolute(self, tmp_path):
        assert _resolve_search_path(str(tmp_path)) == tmp_path
