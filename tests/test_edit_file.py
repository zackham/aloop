"""Tests for curly-to-straight quote normalization in edit_file tool."""

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from aloop.tools.files import _edit_file, _normalize_quotes


def test_normalize_quotes_single():
    assert _normalize_quotes("\u2018hello\u2019") == "'hello'"


def test_normalize_quotes_double():
    assert _normalize_quotes("\u201chello\u201d") == '"hello"'


def test_normalize_quotes_mixed():
    assert _normalize_quotes("\u201cdon\u2019t\u201d") == '"don\'t"'


def test_normalize_quotes_no_curly():
    assert _normalize_quotes("plain 'text' here") == "plain 'text' here"


@pytest.fixture
def tmp_data_file(tmp_path):
    """Create a temp file under a fake data/ path and patch READ_WRITE_PATHS."""
    f = tmp_path / "test.txt"
    return f


def _run(coro):
    return asyncio.run(coro)


class TestEditFileCurlyQuotes:
    """Test that edit_file handles curly quote normalization."""

    def test_exact_match_preferred(self, tmp_data_file):
        """Exact match should be used when available (no normalization needed)."""
        tmp_data_file.write_text("say 'hello' world", encoding="utf-8")
        with patch("aloop.tools.files._check_path", return_value=tmp_data_file):
            result = _run(_edit_file(str(tmp_data_file), "'hello'", "'goodbye'"))
        assert not result.is_error
        assert tmp_data_file.read_text() == "say 'goodbye' world"

    def test_curly_single_quotes_normalized(self, tmp_data_file):
        """Curly single quotes in old_string should match straight quotes in file."""
        tmp_data_file.write_text("say 'hello' world", encoding="utf-8")
        with patch("aloop.tools.files._check_path", return_value=tmp_data_file):
            result = _run(_edit_file(str(tmp_data_file), "\u2018hello\u2019", "'goodbye'"))
        assert not result.is_error
        assert tmp_data_file.read_text() == "say 'goodbye' world"

    def test_curly_double_quotes_normalized(self, tmp_data_file):
        """Curly double quotes in old_string should match straight quotes in file."""
        tmp_data_file.write_text('say "hello" world', encoding="utf-8")
        with patch("aloop.tools.files._check_path", return_value=tmp_data_file):
            result = _run(_edit_file(str(tmp_data_file), "\u201chello\u201d", '"goodbye"'))
        assert not result.is_error
        assert tmp_data_file.read_text() == 'say "goodbye" world'

    def test_file_has_curly_old_string_has_straight(self, tmp_data_file):
        """Straight quotes in old_string should match curly quotes in file."""
        tmp_data_file.write_text("say \u2018hello\u2019 world", encoding="utf-8")
        with patch("aloop.tools.files._check_path", return_value=tmp_data_file):
            result = _run(_edit_file(str(tmp_data_file), "'hello'", "'goodbye'"))
        assert not result.is_error
        # The original curly quotes in the file should be replaced
        assert tmp_data_file.read_text() == "say 'goodbye' world"

    def test_no_match_even_after_normalization(self, tmp_data_file):
        """Should return error when string not found even after normalization."""
        tmp_data_file.write_text("say hello world", encoding="utf-8")
        with patch("aloop.tools.files._check_path", return_value=tmp_data_file):
            result = _run(_edit_file(str(tmp_data_file), "'missing'", "'replacement'"))
        assert result.is_error
        assert "not found" in result.content

    def test_duplicate_after_normalization(self, tmp_data_file):
        """Should return error when normalized match is not unique."""
        tmp_data_file.write_text("say 'hello' and 'hello' twice", encoding="utf-8")
        with patch("aloop.tools.files._check_path", return_value=tmp_data_file):
            result = _run(_edit_file(str(tmp_data_file), "\u2018hello\u2019", "'goodbye'"))
        assert result.is_error
        assert "2 times" in result.content
