"""Tests for JSONC (JSON with comments) support."""

import json
import pytest
from pathlib import Path

from aloop.utils import strip_json_comments, load_jsonc


class TestStripJsonComments:
    """Tests for strip_json_comments."""

    def test_strip_double_slash_comments(self):
        text = '{\n  // this is a comment\n  "key": "value"\n}'
        result = strip_json_comments(text)
        parsed = json.loads(result)
        assert parsed == {"key": "value"}

    def test_strip_hash_comments(self):
        text = '{\n  # this is a comment\n  "key": "value"\n}'
        result = strip_json_comments(text)
        parsed = json.loads(result)
        assert parsed == {"key": "value"}

    def test_preserve_strings_containing_double_slash(self):
        text = '{"url": "https://example.com"}'
        result = strip_json_comments(text)
        parsed = json.loads(result)
        assert parsed == {"url": "https://example.com"}

    def test_preserve_strings_containing_hash(self):
        text = '{"color": "#ff0000", "note": "use # for hex"}'
        result = strip_json_comments(text)
        parsed = json.loads(result)
        assert parsed == {"color": "#ff0000", "note": "use # for hex"}

    def test_multiline_mixed_comments(self):
        text = (
            '{\n'
            '  // double slash comment\n'
            '  "a": 1,\n'
            '  # hash comment\n'
            '  "b": "hello // world",\n'
            '  // another comment\n'
            '  "c": "path/to/#anchor"\n'
            '  # trailing comment\n'
            '}'
        )
        result = strip_json_comments(text)
        parsed = json.loads(result)
        assert parsed == {"a": 1, "b": "hello // world", "c": "path/to/#anchor"}

    def test_empty_input(self):
        result = strip_json_comments("")
        assert result == ""

    def test_no_comments_passthrough(self):
        text = '{"key": "value", "num": 42, "arr": [1, 2, 3]}'
        result = strip_json_comments(text)
        assert json.loads(result) == {"key": "value", "num": 42, "arr": [1, 2, 3]}

    def test_comment_after_value(self):
        text = '{\n  "key": "value" // inline comment\n}'
        result = strip_json_comments(text)
        parsed = json.loads(result)
        assert parsed == {"key": "value"}

    def test_hash_comment_after_value(self):
        text = '{\n  "key": "value" # inline comment\n}'
        result = strip_json_comments(text)
        parsed = json.loads(result)
        assert parsed == {"key": "value"}

    def test_escaped_quotes_in_strings(self):
        text = r'{"msg": "say \"hello // world\"", "ok": true}'
        result = strip_json_comments(text)
        parsed = json.loads(result)
        assert parsed == {"msg": 'say "hello // world"', "ok": True}

    def test_only_comments(self):
        text = '// just a comment\n# another\n{}'
        result = strip_json_comments(text)
        parsed = json.loads(result)
        assert parsed == {}

    def test_commented_out_keys(self):
        """Mimics the scaffolded config.json pattern."""
        text = (
            '{\n'
            '  // "system_prompt": "file:ALOOP-PROMPT.md",\n'
            '  "provider": "openrouter"\n'
            '}'
        )
        result = strip_json_comments(text)
        parsed = json.loads(result)
        assert parsed == {"provider": "openrouter"}

    def test_init_config_template_parses(self):
        """The scaffolded config template should parse cleanly."""
        from aloop.cli import _INIT_CONFIG_TEMPLATE
        result = strip_json_comments(_INIT_CONFIG_TEMPLATE)
        parsed = json.loads(result)
        assert isinstance(parsed, dict)

    def test_preserves_string_with_backslash(self):
        text = r'{"path": "C:\\Users\\test"}'
        result = strip_json_comments(text)
        parsed = json.loads(result)
        assert parsed == {"path": "C:\\Users\\test"}

    def test_comment_at_end_of_file(self):
        text = '{"a": 1}\n// end of file'
        result = strip_json_comments(text)
        parsed = json.loads(result)
        assert parsed == {"a": 1}

    def test_preserves_newlines(self):
        """Comments are stripped but newlines preserved for line tracking."""
        text = '{\n  // comment\n  "key": 1\n}'
        result = strip_json_comments(text)
        assert '\n' in result


class TestLoadJsonc:
    """Tests for load_jsonc."""

    def test_load_nonexistent_file(self, tmp_path):
        result = load_jsonc(tmp_path / "nope.json")
        assert result == {}

    def test_load_valid_jsonc(self, tmp_path):
        f = tmp_path / "config.json"
        f.write_text('{\n  // comment\n  "key": "value"\n}')
        result = load_jsonc(f)
        assert result == {"key": "value"}

    def test_load_invalid_json(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text('{invalid json')
        result = load_jsonc(f)
        assert result == {}

    def test_load_plain_json(self, tmp_path):
        f = tmp_path / "plain.json"
        f.write_text('{"a": 1, "b": [2, 3]}')
        result = load_jsonc(f)
        assert result == {"a": 1, "b": [2, 3]}
