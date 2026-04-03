"""Tests for aloop CLI entry point."""

import io
import json
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock

from aloop.cli import (
    StreamPrinter, JsonStreamPrinter, SilentPrinter,
    parse_args, run_once,
)
from aloop.types import EventType, InferenceEvent


# --- Helpers ---


def _make_event(etype: EventType, data: dict) -> InferenceEvent:
    return InferenceEvent(type=etype, data=data)


async def _mock_stream(*events):
    for e in events:
        yield e


# --- parse_args ---


def test_parse_args_prompt_only(monkeypatch):
    monkeypatch.setattr("sys.argv", ["cli", "hello"])
    args = parse_args()
    assert args.prompt == "hello"
    assert args.model is None
    assert args.session is None
    assert args.print_mode is False
    assert args.continue_last is False
    assert args.resume is None
    assert args.output_format == "text"


def test_parse_args_all_flags(monkeypatch):
    monkeypatch.setattr("sys.argv", [
        "cli", "--model", "m2.5",
        "--session", "s1", "--tools", "bash,read_file",
        "--no-context", "--max-iterations", "10", "-p", "prompt",
    ])
    args = parse_args()
    assert args.model == "m2.5"
    assert args.session == "s1"
    assert args.tools == "bash,read_file"
    assert args.no_context is True
    assert args.max_iterations == 10
    assert args.print_mode is True
    assert args.prompt == "prompt"


def test_parse_args_print_mode(monkeypatch):
    monkeypatch.setattr("sys.argv", ["cli", "-p", "hello"])
    args = parse_args()
    assert args.print_mode is True
    assert args.prompt == "hello"


def test_parse_args_continue(monkeypatch):
    monkeypatch.setattr("sys.argv", ["cli", "-c"])
    args = parse_args()
    assert args.continue_last is True


def test_parse_args_resume(monkeypatch):
    monkeypatch.setattr("sys.argv", ["cli", "--resume", "abc123", "continue this"])
    args = parse_args()
    assert args.resume == "abc123"
    assert args.prompt == "continue this"


def test_parse_args_output_format(monkeypatch):
    monkeypatch.setattr("sys.argv", ["cli", "-o", "stream-json", "-p", "hello"])
    args = parse_args()
    assert args.output_format == "stream-json"


def test_parse_args_output_format_json(monkeypatch):
    monkeypatch.setattr("sys.argv", ["cli", "--output-format", "json", "-p", "hello"])
    args = parse_args()
    assert args.output_format == "json"


def test_parse_args_short_flags(monkeypatch):
    monkeypatch.setattr("sys.argv", ["cli", "-m", "x", "-s", "s1", "-p", "p"])
    args = parse_args()
    assert args.model == "x"
    assert args.session == "s1"
    assert args.print_mode is True
    assert args.prompt == "p"


def test_parse_args_list_models(monkeypatch):
    monkeypatch.setattr("sys.argv", ["cli", "--list-models"])
    args = parse_args()
    assert args.list_models is True


def test_parse_args_no_prompt(monkeypatch):
    monkeypatch.setattr("sys.argv", ["cli"])
    args = parse_args()
    assert args.prompt is None


# --- StreamPrinter ---


def test_on_text_writes_stdout(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = StreamPrinter()
    printer.on_text("hello")
    assert "hello" in buf.getvalue()
    assert printer._in_text is True


def test_on_text_accumulates(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = StreamPrinter()
    printer.on_text("hello ")
    printer.on_text("world")
    assert printer.text == "hello world"


def test_on_text_empty_clears_state(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = StreamPrinter()
    printer.on_text("x")
    assert printer._in_text is True
    printer.on_text("")
    assert printer._in_text is False


def test_on_tool_start_with_args(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = StreamPrinter()
    printer.on_tool_start("bash", {"cmd": "ls"})
    out = buf.getvalue()
    assert "bash" in out
    assert "ls" in out


def test_on_tool_start_no_args(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = StreamPrinter()
    printer.on_tool_start("bash", None)
    out = buf.getvalue()
    assert "bash" in out
    lines = [l for l in out.split("\n") if l.strip()]
    assert not any("\033[33m" in l for l in lines)


def test_on_tool_start_truncates_long_args(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = StreamPrinter()
    printer.on_tool_start("bash", {"cmd": "x" * 200})
    assert "\u2026" in buf.getvalue()


def test_on_tool_start_ends_prior_text(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = StreamPrinter()
    printer.on_text("hi")
    printer.on_tool_start("bash", None)
    out = buf.getvalue()
    hi_idx = out.index("hi")
    bash_idx = out.index("bash")
    between = out[hi_idx + 2:bash_idx]
    assert "\n" in between


def test_on_tool_end_success(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = StreamPrinter()
    printer.on_tool_end("bash", "output text", False)
    out = buf.getvalue()
    assert "\033[32m" in out
    assert "chars" in out
    assert "output text" in out


def test_on_tool_end_error(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = StreamPrinter()
    printer.on_tool_end("bash", "fail msg", True)
    out = buf.getvalue()
    assert "\033[31m" in out
    assert "error" in out


def test_on_tool_end_truncates_long_result(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = StreamPrinter()
    printer.on_tool_end("bash", "y" * 300, False)
    assert "\u2026" in buf.getvalue()


def test_on_turn_nonzero(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = StreamPrinter()
    printer.on_turn(1)
    assert "turn 2" in buf.getvalue()


def test_on_turn_zero(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = StreamPrinter()
    printer.on_turn(0)
    assert buf.getvalue() == ""


def test_on_error_to_stderr(monkeypatch):
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout_buf)
    monkeypatch.setattr("sys.stderr", stderr_buf)
    printer = StreamPrinter()
    printer.on_error("boom")
    assert "boom" in stderr_buf.getvalue()
    assert "\033[31m" in stderr_buf.getvalue()


def test_on_complete_with_usage(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = StreamPrinter()
    printer.on_complete({
        "usage": {
            "model": "m2.5",
            "input_tokens": 100,
            "output_tokens": 50,
            "cost_usd": 0.01,
        }
    })
    out = buf.getvalue()
    assert "m2.5" in out
    assert "100" in out
    assert "0.0100" in out


def test_on_complete_missing_usage(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = StreamPrinter()
    printer.on_complete({})
    assert "?" in buf.getvalue()


# --- JsonStreamPrinter ---


def test_json_stream_text(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = JsonStreamPrinter()
    printer.on_text("hello")
    line = json.loads(buf.getvalue().strip())
    assert line == {"type": "text", "text": "hello"}
    assert printer.text == "hello"


def test_json_stream_tool_start(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = JsonStreamPrinter()
    printer.on_tool_start("bash", {"cmd": "ls"})
    line = json.loads(buf.getvalue().strip())
    assert line["type"] == "tool_start"
    assert line["name"] == "bash"


def test_json_stream_tool_end(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = JsonStreamPrinter()
    printer.on_tool_end("bash", "output", True)
    line = json.loads(buf.getvalue().strip())
    assert line["type"] == "tool_end"
    assert line["is_error"] is True


def test_json_stream_complete(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = JsonStreamPrinter()
    printer.on_complete({"usage": {"model": "test"}})
    line = json.loads(buf.getvalue().strip())
    assert line["type"] == "complete"
    assert line["usage"]["model"] == "test"


def test_json_stream_error(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = JsonStreamPrinter()
    printer.on_error("fail")
    line = json.loads(buf.getvalue().strip())
    assert line == {"type": "error", "message": "fail"}


# --- SilentPrinter ---


def test_silent_collects_text():
    printer = SilentPrinter()
    printer.on_text("hello ")
    printer.on_text("world")
    assert printer.text == "hello world"


def test_silent_no_output(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = SilentPrinter()
    printer.on_text("hello")
    printer.on_tool_start("bash", {"x": 1})
    printer.on_tool_end("bash", "result", False)
    printer.on_turn(0)
    assert buf.getvalue() == ""


def test_silent_print_result(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    printer = SilentPrinter()
    printer.on_text("response text")
    printer.on_complete({"usage": {"input_tokens": 100}, "cost_usd": 0.01})
    printer.print_result("sess-123")
    result = json.loads(buf.getvalue().strip())
    assert result["text"] == "response text"
    assert result["session_id"] == "sess-123"
    assert result["cost_usd"] == 0.01


def test_silent_error_clears_text():
    printer = SilentPrinter()
    printer.on_text("partial")
    printer.on_error("oops")
    assert printer.text == ""


# --- run_once ---


@pytest.mark.asyncio
async def test_run_once_happy_path():
    events = [
        _make_event(EventType.TEXT_DELTA, {"text": "hello"}),
        _make_event(EventType.TOOL_START, {"name": "bash", "args": {"cmd": "ls"}}),
        _make_event(EventType.TOOL_END, {"name": "bash", "result": "file.txt", "is_error": False}),
        _make_event(EventType.COMPLETE, {"usage": {"model": "test"}}),
    ]
    backend = MagicMock()
    backend.stream = lambda prompt, **kw: _mock_stream(*events)

    printer = MagicMock(spec=StreamPrinter)
    result = await run_once(backend, "test prompt", printer)

    printer.on_text.assert_called_with("hello")
    printer.on_tool_start.assert_called_once_with("bash", {"cmd": "ls"})
    printer.on_tool_end.assert_called_once_with("bash", "file.txt", False)
    printer.on_complete.assert_called_once()
    assert result == {"usage": {"model": "test"}}


@pytest.mark.asyncio
async def test_run_once_error_returns_none():
    events = [
        _make_event(EventType.ERROR, {"message": "something broke"}),
    ]
    backend = MagicMock()
    backend.stream = lambda prompt, **kw: _mock_stream(*events)

    printer = MagicMock(spec=StreamPrinter)
    result = await run_once(backend, "test prompt", printer)

    assert result is None
    printer.on_error.assert_called_once_with("something broke")


@pytest.mark.asyncio
async def test_run_once_text_then_complete():
    events = [
        _make_event(EventType.TEXT_DELTA, {"text": "just text"}),
        _make_event(EventType.COMPLETE, {"text": "done"}),
    ]
    backend = MagicMock()
    backend.stream = lambda prompt, **kw: _mock_stream(*events)

    printer = MagicMock(spec=StreamPrinter)
    result = await run_once(backend, "test", printer)

    printer.on_text.assert_called_with("just text")
    printer.on_complete.assert_called_once()
    assert result == {"text": "done"}


@pytest.mark.asyncio
async def test_run_once_keyboard_interrupt(monkeypatch):
    stderr_buf = io.StringIO()
    monkeypatch.setattr("sys.stderr", stderr_buf)

    async def raising_stream(prompt, **kw):
        yield _make_event(EventType.TEXT_DELTA, {"text": "start"})
        raise KeyboardInterrupt

    backend = MagicMock()
    backend.stream = raising_stream

    printer = MagicMock(spec=StreamPrinter)
    result = await run_once(backend, "test", printer)

    assert result is None
    printer.flush.assert_called_once()
    assert "interrupted" in stderr_buf.getvalue()


@pytest.mark.asyncio
async def test_run_once_turn_start():
    events = [
        _make_event(EventType.TURN_START, {"iteration": 1}),
        _make_event(EventType.COMPLETE, {"text": "done"}),
    ]
    backend = MagicMock()
    backend.stream = lambda prompt, **kw: _mock_stream(*events)

    printer = MagicMock(spec=StreamPrinter)
    await run_once(backend, "test", printer)

    printer.on_turn.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_run_once_no_events():
    backend = MagicMock()
    backend.stream = lambda prompt, **kw: _mock_stream()

    printer = MagicMock(spec=StreamPrinter)
    result = await run_once(backend, "test", printer)

    assert result is None
    printer.flush.assert_called_once()
