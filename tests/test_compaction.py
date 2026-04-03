"""Tests for aloop context compaction."""

import json
import os
import time

from aloop.compaction import (
    PERSISTED_OUTPUT_TAG,
    PERSIST_EXEMPT_TOOLS,
    CompactionSettings,
    FileOperations,
    _get_compaction_config_path,
    _serialize_for_summary,
    estimate_message_tokens,
    extract_file_ops,
    find_cut_point,
    get_compaction_settings,
    persist_tool_result,
    restore_recent_files,
    should_compact,
)


def test_estimate_tokens():
    msg = {"role": "user", "content": "Hello world"}
    assert estimate_message_tokens(msg) == 3


def test_should_compact():
    settings = CompactionSettings(reserve_tokens=1000)
    assert should_compact(9500, 10000, settings) is True
    assert should_compact(8000, 10000, settings) is False
    assert should_compact(9500, 10000, CompactionSettings(enabled=False)) is False


def test_find_cut_point():
    messages = [{"role": "user", "content": "x" * 100} for _ in range(5)]
    cut = find_cut_point(messages, keep_recent_tokens=50)
    assert cut == 3


def test_find_cut_point_skips_tool_results():
    messages = [
        {"role": "user", "content": "x" * 100},
        {
            "role": "assistant",
            "content": "y" * 100,
            "tool_calls": [
                {
                    "id": "1",
                    "function": {"name": "bash", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "content": "z" * 100},
        {"role": "assistant", "content": "w" * 100},
    ]
    cut = find_cut_point(messages, keep_recent_tokens=25)
    assert messages[cut].get("role") != "tool"


def test_extract_file_ops():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "/data/foo.json"}',
                    }
                },
                {
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path": "/data/bar.json", "content": "..."}',
                    }
                },
            ],
        },
    ]
    ops = extract_file_ops(messages)
    assert "/data/foo.json" in ops.read
    assert "/data/bar.json" in ops.written


def test_extract_file_ops_cumulative():
    prev = FileOperations(read={"/old.json"}, written={"/prev.json"})
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "/new.json"}',
                    }
                },
            ],
        }
    ]
    ops = extract_file_ops(messages, prev)
    assert "/old.json" in ops.read
    assert "/new.json" in ops.read
    assert "/prev.json" in ops.written


def test_serialize_for_summary():
    messages = [
        {"role": "user", "content": "Do something"},
        {
            "role": "assistant",
            "content": "OK",
            "tool_calls": [
                {"function": {"name": "bash", "arguments": '{"command": "ls"}'}}
            ],
        },
        {"role": "tool", "content": "file1.txt\nfile2.txt"},
    ]
    text = _serialize_for_summary(messages)
    assert "[User]: Do something" in text
    assert "[Assistant]: OK" in text
    assert "[Tool result]:" in text


# ---------- persist_tool_result ----------


def test_persist_small_result():
    content = "short output"
    assert persist_tool_result(content, "bash", "tc1") == content


def test_persist_large_result_to_disk(tmp_path):
    content = "x" * 60_000
    result = persist_tool_result(
        content, "bash", "tc1", overflow_dir=tmp_path, max_chars=50_000,
    )
    assert PERSISTED_OUTPUT_TAG in result
    assert "60,000 chars" in result
    # File was written
    files = list(tmp_path.glob("tc1.*"))
    assert len(files) == 1
    assert files[0].read_text() == content
    # Preview is head-only, ~2K
    assert len(result) < 5_000


def test_persist_json_gets_json_extension(tmp_path):
    content = json.dumps({"key": "x" * 60_000})
    persist_tool_result(
        content, "bash", "tc2", overflow_dir=tmp_path, max_chars=50_000,
    )
    files = list(tmp_path.glob("tc2.*"))
    assert files[0].suffix == ".json"


def test_persist_text_gets_txt_extension(tmp_path):
    content = "plain text " * 10_000
    persist_tool_result(
        content, "bash", "tc3", overflow_dir=tmp_path, max_chars=50_000,
    )
    files = list(tmp_path.glob("tc3.*"))
    assert files[0].suffix == ".txt"


def test_persist_exempt_tool_returns_as_is():
    """read_file results are never persisted (circular)."""
    content = "x" * 60_000
    result = persist_tool_result(content, "read_file", "tc4", max_chars=50_000)
    assert result == content
    assert "read_file" in PERSIST_EXEMPT_TOOLS


def test_persist_no_overflow_dir_still_previews():
    content = "x" * 60_000
    result = persist_tool_result(
        content, "bash", "tc5", overflow_dir=None, max_chars=50_000,
    )
    assert PERSISTED_OUTPUT_TAG in result
    assert "No session directory" in result
    assert len(result) < 5_000


def test_persist_preview_cuts_at_newline():
    lines = "\n".join(f"line {i}" for i in range(500))
    content = lines + "x" * 60_000
    result = persist_tool_result(
        content, "bash", "tc6", overflow_dir=None, max_chars=50_000,
    )
    # Preview should end at a newline, not mid-word
    preview_section = result.split("Preview")[1]
    # The preview text before "..." should end at a line break
    assert "\n...\n" in result


# ---------- restore_recent_files ----------


def test_restore_reads_existing(tmp_path):
    f1 = tmp_path / "edited.py"
    f1.write_text("def foo(): pass")
    f2 = tmp_path / "written.py"
    f2.write_text("def bar(): pass")
    f3 = tmp_path / "read.py"
    f3.write_text("def baz(): pass")

    file_ops = {
        "edited": [str(f1)],
        "written": [str(f2)],
        "read": [str(f3)],
    }

    result = restore_recent_files(file_ops)
    assert len(result) == 1
    msg = result[0]
    assert msg["role"] == "user"
    assert "def foo(): pass" in msg["content"]


def test_restore_sorts_by_mtime(tmp_path):
    """Most recently modified files should appear first."""
    old = tmp_path / "old.py"
    old.write_text("old content")
    # Ensure different mtimes
    time.sleep(0.05)
    new = tmp_path / "new.py"
    new.write_text("new content")

    file_ops = {
        "edited": [],
        "written": [],
        "read": [str(old), str(new)],
    }

    result = restore_recent_files(file_ops)
    content = result[0]["content"]
    # new.py should appear before old.py
    assert content.index("new.py") < content.index("old.py")


def test_restore_deduplicates_against_kept(tmp_path):
    """Files already visible in kept messages should be skipped."""
    f1 = tmp_path / "visible.py"
    f1.write_text("already in context")
    f2 = tmp_path / "needs_restore.py"
    f2.write_text("not in context")

    file_ops = {
        "edited": [],
        "written": [],
        "read": [str(f1), str(f2)],
    }

    # Simulate kept messages that already contain a read_file for f1
    kept_messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": str(f1)}),
                    }
                }
            ],
        },
    ]

    result = restore_recent_files(file_ops, kept_messages=kept_messages)
    content = result[0]["content"]
    assert "needs_restore.py" in content
    assert "visible.py" not in content


def test_restore_enforces_total_budget(tmp_path):
    """Total token budget should limit how many files are included."""
    files = []
    for i in range(5):
        f = tmp_path / f"file_{i}.py"
        f.write_text("x" * 5_000)
        files.append(str(f))
        time.sleep(0.01)

    file_ops = {"edited": [], "written": [], "read": files}

    # Very tight budget — should include fewer than 5
    result = restore_recent_files(
        file_ops, total_budget_chars=8_000,
    )
    content = result[0]["content"]
    # Should have at most 1 file (each block is ~5K + markup)
    file_count = content.count("<file path=")
    assert file_count < 5


def test_restore_max_files_limit(tmp_path):
    files = []
    for i in range(8):
        f = tmp_path / f"file_{i}.py"
        f.write_text(f"content_{i}")
        files.append(str(f))

    file_ops = {"edited": [], "written": [], "read": files}
    result = restore_recent_files(file_ops, max_files=3)
    content = result[0]["content"]
    file_count = content.count("<file path=")
    assert file_count == 3


def test_restore_truncates_large_files(tmp_path):
    f = tmp_path / "big.py"
    f.write_text("x" * 50_000)

    result = restore_recent_files(
        {"edited": [str(f)], "written": [], "read": []},
        max_chars_per_file=1000,
    )
    content = result[0]["content"]
    assert "[... truncated]" in content


def test_restore_handles_missing_files():
    result = restore_recent_files(
        {"edited": ["/nonexistent/file.py"], "written": [], "read": []},
    )
    assert result == []


def test_restore_empty_ops():
    result = restore_recent_files({"edited": [], "written": [], "read": []})
    assert result == []


# ---------- get_compaction_settings ----------


def test_settings_global_defaults():
    settings = get_compaction_settings()
    assert settings.reserve_tokens == 16_384
    assert settings.keep_recent_tokens == 20_000
    assert settings.compact_instructions is None


def test_settings_json_override(tmp_path, monkeypatch):
    config = tmp_path / "compaction.json"
    config.write_text(json.dumps({
        "reserve_tokens": 4096,
        "keep_recent_tokens": 50_000,
        "compact_instructions": "keep it short",
    }))
    monkeypatch.setattr(
        "aloop.compaction._get_compaction_config_path", lambda: config
    )

    settings = get_compaction_settings()
    assert settings.reserve_tokens == 4096
    assert settings.keep_recent_tokens == 50_000
    assert settings.compact_instructions == "keep it short"


def test_settings_json_missing_file():
    # Should not crash if config file doesn't exist
    settings = get_compaction_settings()
    assert settings.reserve_tokens == 16_384
