"""Runtime tests for the `aloop complete` CLI subcommand.

Covers stdin handling, prompt assembly, mode resolution, output formats,
and error paths. Mocks ALoop.complete so no network calls happen.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aloop.cli import _cmd_complete, parse_args
from aloop.types import InferenceError, RunResult


# --- Helpers ---------------------------------------------------------------


def _fake_run_result(text: str = "ok", model: str = "test/model") -> RunResult:
    return RunResult(
        text=text,
        input_tokens=3,
        output_tokens=5,
        cost_usd=0.000042,
        model=model,
        turns=1,
    )


class _FakeStdin(io.StringIO):
    def __init__(self, content: str, tty: bool):
        super().__init__(content)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture
def fake_env(monkeypatch):
    """Give ALoop a model via env var so provider/model resolution succeeds."""
    monkeypatch.setenv("ALOOP_MODEL", "test/model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key")
    yield


def _patch_stdin(monkeypatch, content: str, tty: bool) -> None:
    monkeypatch.setattr("sys.stdin", _FakeStdin(content, tty))


def _capture_stdio(monkeypatch):
    out = io.StringIO()
    err = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)
    return out, err


# --- Stdin handling & prompt assembly --------------------------------------


@pytest.mark.asyncio
async def test_positional_no_stdin_uses_positional_only(monkeypatch, fake_env):
    _patch_stdin(monkeypatch, "", tty=True)
    out, err = _capture_stdio(monkeypatch)

    mock = AsyncMock(return_value=_fake_run_result("hello-world"))
    with patch("aloop.agent_backend.ALoop.complete", mock):
        args = parse_args(["complete", "say hi"])
        rc = await _cmd_complete(args)

    assert rc == 0
    mock.assert_awaited_once()
    # First positional arg to ALoop.complete is the prompt
    call_args, call_kwargs = mock.call_args
    assert call_args[0] == "say hi"
    assert "hello-world" in out.getvalue()


@pytest.mark.asyncio
async def test_positional_plus_piped_stdin_combines(monkeypatch, fake_env):
    _patch_stdin(monkeypatch, "article body here\n", tty=False)
    out, err = _capture_stdio(monkeypatch)

    mock = AsyncMock(return_value=_fake_run_result())
    with patch("aloop.agent_backend.ALoop.complete", mock):
        args = parse_args(["complete", "summarize this:"])
        rc = await _cmd_complete(args)

    assert rc == 0
    call_args, _ = mock.call_args
    assert call_args[0] == "summarize this:\n\narticle body here"


@pytest.mark.asyncio
async def test_no_positional_piped_stdin_uses_stdin(monkeypatch, fake_env):
    _patch_stdin(monkeypatch, "just this content\n", tty=False)
    out, err = _capture_stdio(monkeypatch)

    mock = AsyncMock(return_value=_fake_run_result())
    with patch("aloop.agent_backend.ALoop.complete", mock):
        args = parse_args(["complete"])
        rc = await _cmd_complete(args)

    assert rc == 0
    call_args, _ = mock.call_args
    assert call_args[0] == "just this content"


@pytest.mark.asyncio
async def test_no_positional_interactive_errors(monkeypatch, fake_env):
    _patch_stdin(monkeypatch, "", tty=True)
    out, err = _capture_stdio(monkeypatch)

    mock = AsyncMock(return_value=_fake_run_result())
    with patch("aloop.agent_backend.ALoop.complete", mock):
        args = parse_args(["complete"])
        rc = await _cmd_complete(args)

    assert rc == 1
    assert "no prompt provided" in err.getvalue()
    mock.assert_not_awaited()


# --- Output formats --------------------------------------------------------


@pytest.mark.asyncio
async def test_output_text_prints_text_only(monkeypatch, fake_env):
    _patch_stdin(monkeypatch, "", tty=True)
    out, err = _capture_stdio(monkeypatch)

    mock = AsyncMock(return_value=_fake_run_result("the answer"))
    with patch("aloop.agent_backend.ALoop.complete", mock):
        args = parse_args(["complete", "ignored"])
        rc = await _cmd_complete(args)

    assert rc == 0
    # stdout has just the text (with optional trailing newline)
    assert out.getvalue().rstrip("\n") == "the answer"
    # No JSON noise in stdout
    assert "input_tokens" not in out.getvalue()


@pytest.mark.asyncio
async def test_output_json_prints_structured_blob(monkeypatch, fake_env):
    _patch_stdin(monkeypatch, "", tty=True)
    out, err = _capture_stdio(monkeypatch)

    mock = AsyncMock(return_value=_fake_run_result("the answer", "fake/model"))
    with patch("aloop.agent_backend.ALoop.complete", mock):
        args = parse_args(["complete", "-o", "json", "ignored"])
        rc = await _cmd_complete(args)

    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload == {
        "text": "the answer",
        "input_tokens": 3,
        "output_tokens": 5,
        "cost_usd": 0.000042,
        "model": "fake/model",
    }


# --- Mode resolution -------------------------------------------------------


@pytest.fixture
def tmp_mode_project(tmp_path: Path, monkeypatch):
    """Create a tmp project with .aloop/config.json containing modes."""
    aloop_dir = tmp_path / ".aloop"
    aloop_dir.mkdir()
    config = {
        "modes": {
            "just_model": {
                "model": "fake/mode-model",
            },
            "with_sp": {
                "model": "fake/mode-model-sp",
                "system_prompt": "you are a mode-defined assistant",
            },
        }
    }
    (aloop_dir / "config.json").write_text(json.dumps(config))
    monkeypatch.setenv("ALOOP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key")
    yield tmp_path


@pytest.mark.asyncio
async def test_mode_resolves_model(monkeypatch, tmp_mode_project):
    _patch_stdin(monkeypatch, "", tty=True)
    out, err = _capture_stdio(monkeypatch)

    mock = AsyncMock(return_value=_fake_run_result())

    captured = {}

    def _fake_init(self, *, model, provider):
        captured["model"] = model
        captured["provider"] = provider
        # Minimal init so .complete() can be mocked
        self.api_key = "sk-test-key"

    with patch("aloop.cli.ALoop.__init__", _fake_init), \
            patch("aloop.cli.ALoop.complete", mock):
        args = parse_args(["complete", "--mode", "just_model", "hi"])
        rc = await _cmd_complete(args)

    assert rc == 0
    assert captured["model"] == "fake/mode-model"


@pytest.mark.asyncio
async def test_mode_system_prompt_default(monkeypatch, tmp_mode_project):
    _patch_stdin(monkeypatch, "", tty=True)
    out, err = _capture_stdio(monkeypatch)

    mock = AsyncMock(return_value=_fake_run_result())

    def _fake_init(self, *, model, provider):
        self.api_key = "sk-test-key"

    with patch("aloop.cli.ALoop.__init__", _fake_init), \
            patch("aloop.cli.ALoop.complete", mock):
        args = parse_args(["complete", "--mode", "with_sp", "hi"])
        rc = await _cmd_complete(args)

    assert rc == 0
    _, call_kwargs = mock.call_args
    assert call_kwargs["system_prompt"] == "you are a mode-defined assistant"


@pytest.mark.asyncio
async def test_caller_system_prompt_beats_mode(monkeypatch, tmp_mode_project):
    _patch_stdin(monkeypatch, "", tty=True)
    out, err = _capture_stdio(monkeypatch)

    mock = AsyncMock(return_value=_fake_run_result())

    def _fake_init(self, *, model, provider):
        self.api_key = "sk-test-key"

    with patch("aloop.cli.ALoop.__init__", _fake_init), \
            patch("aloop.cli.ALoop.complete", mock):
        args = parse_args([
            "complete",
            "--mode", "with_sp",
            "--system-prompt", "caller wins",
            "hi",
        ])
        rc = await _cmd_complete(args)

    assert rc == 0
    _, call_kwargs = mock.call_args
    assert call_kwargs["system_prompt"] == "caller wins"


@pytest.mark.asyncio
async def test_unknown_mode_errors(monkeypatch, tmp_mode_project):
    _patch_stdin(monkeypatch, "", tty=True)
    out, err = _capture_stdio(monkeypatch)

    mock = AsyncMock(return_value=_fake_run_result())
    with patch("aloop.agent_backend.ALoop.complete", mock):
        args = parse_args(["complete", "--mode", "does_not_exist", "hi"])
        rc = await _cmd_complete(args)

    assert rc == 1
    assert "Unknown mode" in err.getvalue()
    mock.assert_not_awaited()


# --- JSON mode shorthand ---------------------------------------------------


@pytest.mark.asyncio
async def test_json_flag_sets_response_format(monkeypatch, fake_env):
    _patch_stdin(monkeypatch, "", tty=True)
    out, err = _capture_stdio(monkeypatch)

    mock = AsyncMock(return_value=_fake_run_result('{"k": "v"}'))
    with patch("aloop.agent_backend.ALoop.complete", mock):
        args = parse_args(["complete", "--json", "list 3 things"])
        rc = await _cmd_complete(args)

    assert rc == 0
    _, call_kwargs = mock.call_args
    assert call_kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_temperature_and_max_tokens_passed_through(monkeypatch, fake_env):
    _patch_stdin(monkeypatch, "", tty=True)
    out, err = _capture_stdio(monkeypatch)

    mock = AsyncMock(return_value=_fake_run_result())
    with patch("aloop.agent_backend.ALoop.complete", mock):
        args = parse_args([
            "complete", "--temperature", "0.3", "--max-tokens", "500", "hi",
        ])
        rc = await _cmd_complete(args)

    assert rc == 0
    _, call_kwargs = mock.call_args
    assert call_kwargs["temperature"] == 0.3
    assert call_kwargs["max_tokens"] == 500


# --- System prompt file ----------------------------------------------------


@pytest.mark.asyncio
async def test_system_prompt_file_read(monkeypatch, fake_env, tmp_path):
    sp_file = tmp_path / "sp.md"
    sp_file.write_text("be very concise")
    _patch_stdin(monkeypatch, "", tty=True)
    out, err = _capture_stdio(monkeypatch)

    mock = AsyncMock(return_value=_fake_run_result())
    with patch("aloop.agent_backend.ALoop.complete", mock):
        args = parse_args([
            "complete", "--system-prompt-file", str(sp_file), "hi",
        ])
        rc = await _cmd_complete(args)

    assert rc == 0
    _, call_kwargs = mock.call_args
    assert call_kwargs["system_prompt"] == "be very concise"


@pytest.mark.asyncio
async def test_system_prompt_file_missing_errors(monkeypatch, fake_env):
    _patch_stdin(monkeypatch, "", tty=True)
    out, err = _capture_stdio(monkeypatch)

    mock = AsyncMock(return_value=_fake_run_result())
    with patch("aloop.agent_backend.ALoop.complete", mock):
        args = parse_args([
            "complete", "--system-prompt-file", "/nonexistent/path.md", "hi",
        ])
        rc = await _cmd_complete(args)

    assert rc == 1
    assert "system prompt file not found" in err.getvalue()
    mock.assert_not_awaited()


# --- Error paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_inference_error_exits_one(monkeypatch, fake_env):
    _patch_stdin(monkeypatch, "", tty=True)
    out, err = _capture_stdio(monkeypatch)

    mock = AsyncMock(side_effect=InferenceError("provider exploded"))
    with patch("aloop.agent_backend.ALoop.complete", mock):
        args = parse_args(["complete", "hi"])
        rc = await _cmd_complete(args)

    assert rc == 1
    assert "provider exploded" in err.getvalue()


@pytest.mark.asyncio
async def test_missing_model_errors(monkeypatch):
    # No ALOOP_MODEL env, no --model, no --mode -> error
    monkeypatch.delenv("ALOOP_MODEL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key")
    _patch_stdin(monkeypatch, "", tty=True)
    out, err = _capture_stdio(monkeypatch)

    mock = AsyncMock(return_value=_fake_run_result())
    with patch("aloop.agent_backend.ALoop.complete", mock):
        args = parse_args(["complete", "hi"])
        rc = await _cmd_complete(args)

    assert rc == 1
    assert "no model specified" in err.getvalue()
    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_format_raw_json(monkeypatch, fake_env):
    _patch_stdin(monkeypatch, "", tty=True)
    out, err = _capture_stdio(monkeypatch)

    mock = AsyncMock(return_value=_fake_run_result())
    with patch("aloop.agent_backend.ALoop.complete", mock):
        args = parse_args([
            "complete", "--response-format", '{"type": "json_object"}', "hi",
        ])
        rc = await _cmd_complete(args)

    assert rc == 0
    _, call_kwargs = mock.call_args
    assert call_kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_response_format_invalid_json_errors(monkeypatch, fake_env):
    _patch_stdin(monkeypatch, "", tty=True)
    out, err = _capture_stdio(monkeypatch)

    mock = AsyncMock(return_value=_fake_run_result())
    with patch("aloop.agent_backend.ALoop.complete", mock):
        args = parse_args(["complete", "--response-format", "not-json", "hi"])
        rc = await _cmd_complete(args)

    assert rc == 1
    assert "not valid JSON" in err.getvalue()
    mock.assert_not_awaited()
