"""aloop CLI — agent loop with tool access, skills, and hooks.

Usage:
    aloop "prompt"                        # interactive session (auto-created)
    aloop run "prompt"                    # explicit run subcommand
    aloop -p "prompt"                     # one-shot, print and exit
    aloop -c                              # continue last session
    aloop --resume SESSION_ID "prompt"    # resume a specific session
    aloop --model x-ai/grok-4.1-fast "prompt"
    echo "prompt" | aloop                 # pipe (one-shot)
    aloop serve                           # ACP server (replaces --acp)
    aloop config show                     # show resolved configuration
    aloop providers list                  # list available providers
    aloop providers validate              # test a provider
    aloop update                          # self-update
    aloop init                            # scaffold .aloop/ directory
    aloop version                         # print version
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

from .agent_backend import ALoop
from .models import get_models
from .system_prompt import build_system_prompt
from .types import EventType

# ANSI escape codes
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"
_RESET = "\033[0m"

_STATE_FILE = Path.home() / ".aloop" / "state.json"

# Known subcommands — used for implicit "run" injection
SUBCOMMANDS = {
    "run", "complete", "serve", "config", "providers", "update",
    "register-acpx", "init", "version", "system-prompt", "sessions",
}


def _load_state() -> dict:
    from .utils import load_jsonc
    return load_jsonc(_STATE_FILE)


def _save_state(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments using subparsers.

    If the first positional argument is not a known subcommand and doesn't
    start with '-', inject 'run' as the implicit default subcommand.
    """
    # Work on a copy of argv to avoid mutating the real sys.argv
    if argv is None:
        args_list = sys.argv[1:]
    else:
        args_list = list(argv)

    # Implicit "run" injection: if first arg is not a known subcommand
    # and not a flag, treat it as a prompt for the "run" subcommand.
    # Also inject "run" when there are no args at all (interactive mode).
    if not args_list or (
        args_list[0] not in SUBCOMMANDS
        and not args_list[0].startswith("-")
    ):
        args_list.insert(0, "run")
    # Handle bare flags (e.g. "aloop -p hello", "aloop -c", "aloop --version")
    elif args_list[0].startswith("-") and args_list[0] not in ("--version", "--help", "-h"):
        args_list.insert(0, "run")

    parser = argparse.ArgumentParser(
        description="Agent loop with tool access, skills, and hooks",
        prog="aloop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "subcommands:\n"
            "  run                  Run a prompt (default when bare prompt given)\n"
            "  serve                Run as ACP server over stdio\n"
            "  config show          Show resolved configuration\n"
            "  config validate      Validate config files (JSONC parsing)\n"
            "  providers list       List available API providers\n"
            "  providers validate   Test a provider's API compatibility\n"
            "  update               Self-update to latest version\n"
            "  register-acpx        Register aloop with acpx for ACP integration\n"
            "  init                 Scaffold .aloop/ directory\n"
            "  version              Print version and exit\n"
            "  system-prompt        Show system prompt template"
        ),
    )
    parser.add_argument("--version", action="store_true", help="Show version and exit")

    subparsers = parser.add_subparsers(dest="subcommand")

    # --- run ---
    run_parser = subparsers.add_parser("run", help="Run a prompt (default)")
    run_parser.add_argument("prompt", nargs="?",
                            help="Prompt text")
    run_parser.add_argument("-p", action="store_true", dest="print_mode",
                            help="One-shot: print response and exit (no REPL)")
    run_parser.add_argument("-c", "--continue", action="store_true", dest="continue_last",
                            help="Continue last session")
    run_parser.add_argument("--resume", metavar="SESSION_ID",
                            help="Resume a specific session by ID")
    run_parser.add_argument("-s", "--session", default=None,
                            help="Use a named session instead of auto-generated ID")
    run_parser.add_argument("--output-format", "-o", default="text",
                            choices=["text", "json", "stream-json"],
                            help="Output format (default: text)")
    run_parser.add_argument("--model", "-m", default=None,
                            help="Model ID (e.g. x-ai/grok-4.1-fast)")
    run_parser.add_argument("--provider", default=None,
                            help="API provider (default: openrouter)")
    run_parser.add_argument("--mode", default=None,
                            help="Named mode from .aloop/config.json modes section")
    run_parser.add_argument("--system-prompt", default=None, dest="system_prompt_override",
                            help="Override system prompt text")
    run_parser.add_argument("--system-prompt-file", default=None, dest="system_prompt_file",
                            help="Override system prompt from file")
    run_parser.add_argument("--tools", default=None,
                            help="Comma-separated tool names")
    run_parser.add_argument("--no-context", action="store_true",
                            help="Skip context injection")
    run_parser.add_argument("--max-iterations", type=int, default=50,
                            help="Max agent loop iterations (default: 50)")
    run_parser.add_argument("--thinking", default=None,
                            choices=["enabled", "disabled"],
                            help="Reasoning toggle for thinking-capable models (e.g. DeepSeek V4)")
    run_parser.add_argument("--reasoning-effort", default=None, dest="reasoning_effort",
                            choices=["high", "max"],
                            help="Reasoning effort level for thinking-capable models")

    # --- complete ---
    complete_parser = subparsers.add_parser(
        "complete",
        help="One-shot inference (no tools, no session, no agent loop)",
    )
    complete_parser.add_argument("prompt", nargs="?",
                                 help="Prompt text (if omitted, read from stdin)")
    complete_parser.add_argument("--model", "-m", default=None,
                                 help="Model ID (e.g. google/gemini-2.5-flash)")
    complete_parser.add_argument("--provider", default=None,
                                 help="API provider (default: openrouter)")
    complete_parser.add_argument("--mode", default=None,
                                 help="Named mode from .aloop/config.json (only 'model' + 'system_prompt' are consulted)")
    complete_parser.add_argument("--system-prompt", default=None, dest="system_prompt_override",
                                 help="System prompt text")
    complete_parser.add_argument("--system-prompt-file", default=None, dest="system_prompt_file",
                                 help="Read system prompt from file")
    complete_parser.add_argument("--temperature", type=float, default=None,
                                 help="Sampling temperature")
    complete_parser.add_argument("--max-tokens", type=int, default=None, dest="max_tokens",
                                 help="Max output tokens")
    complete_parser.add_argument("--json", action="store_true", dest="json_mode",
                                 help="Shorthand for --response-format '{\"type\": \"json_object\"}'")
    complete_parser.add_argument("--response-format", default=None, dest="response_format",
                                 help="Raw response_format JSON (overrides --json)")
    complete_parser.add_argument("--output-format", "-o", default="text",
                                 choices=["text", "json"],
                                 help="Output format: 'text' prints result.text; 'json' prints a one-line JSON blob")
    complete_parser.add_argument("--thinking", default=None,
                                 choices=["enabled", "disabled"],
                                 help="Reasoning toggle for thinking-capable models")
    complete_parser.add_argument("--reasoning-effort", default=None, dest="reasoning_effort",
                                 choices=["high", "max"],
                                 help="Reasoning effort level for thinking-capable models")

    # --- serve ---
    serve_parser = subparsers.add_parser("serve", help="Run as ACP server over stdio")
    serve_parser.add_argument("--model", "-m", default=None,
                              help="Model ID")
    serve_parser.add_argument("--provider", default=None,
                              help="API provider")

    # --- config ---
    config_parser = subparsers.add_parser("config", help="Configuration management")
    config_sub = config_parser.add_subparsers(dest="config_action")
    config_sub.add_parser("show", help="Show resolved configuration")
    config_sub.add_parser("validate", help="Validate config files (JSONC parsing)")

    # --- providers ---
    providers_parser = subparsers.add_parser("providers", help="Provider management")
    providers_sub = providers_parser.add_subparsers(dest="providers_action")
    providers_sub.add_parser("list", help="List available providers")
    validate_parser = providers_sub.add_parser("validate", help="Test a provider")
    validate_parser.add_argument("--provider", required=True,
                                 help="Provider to test")
    validate_parser.add_argument("--model", "-m", required=True,
                                 help="Model to test with")

    # --- update ---
    subparsers.add_parser("update", help="Self-update to latest version")

    # --- register-acpx ---
    subparsers.add_parser("register-acpx", help="Register aloop with acpx for ACP integration")

    # --- init ---
    subparsers.add_parser("init", help="Scaffold .aloop/ directory")

    # --- version ---
    subparsers.add_parser("version", help="Print version and exit")

    # --- system-prompt ---
    sp_parser = subparsers.add_parser("system-prompt", help="Show system prompt template")
    sp_parser.add_argument("--rendered", action="store_true",
                           help="Show fully interpolated prompt")

    # --- sessions ---
    sessions_parser = subparsers.add_parser("sessions", help="Session management")
    sessions_sub = sessions_parser.add_subparsers(dest="sessions_action")
    sessions_sub.add_parser("list", help="List all sessions with fork metadata")
    sessions_info_parser = sessions_sub.add_parser("info", help="Show session details")
    sessions_info_parser.add_argument("session_id", help="Session ID to inspect")
    sessions_gc_parser = sessions_sub.add_parser("gc", help="Garbage-collect expired sessions")
    sessions_gc_parser.add_argument("--max-age", type=int, default=604800,
                                    help="Max age in seconds (default: 7 days)")
    sessions_mat_parser = sessions_sub.add_parser("materialize", help="Materialize a forked session")
    sessions_mat_parser.add_argument("session_id", help="Session ID to materialize")
    sessions_sub.add_parser("rebuild-index", help="Rebuild the fork index cache")

    return parser.parse_args(args_list)


# ---------------------------------------------------------------------------
# Output adapters
# ---------------------------------------------------------------------------

class StreamPrinter:
    """ANSI-formatted terminal output (--output-format text)."""

    def __init__(self):
        self._in_text = False
        self._in_thinking = False
        self._accumulated = ""

    def _end_text(self):
        if self._in_text:
            sys.stdout.write("\n")
            self._in_text = False

    def _end_thinking(self):
        if self._in_thinking:
            sys.stdout.write(f"{_RESET}\n{_DIM}└─ thinking ─┘{_RESET}\n\n")
            self._in_thinking = False

    def on_text(self, text: str):
        if self._in_thinking:
            self._end_thinking()
        sys.stdout.write(text)
        sys.stdout.flush()
        self._in_text = bool(text)
        self._accumulated += text

    def on_thinking_start(self):
        self._end_text()
        sys.stdout.write(f"{_DIM}┌─ thinking ─┐\n{_RESET}")
        sys.stdout.flush()
        self._in_thinking = True

    def on_thinking_delta(self, text: str):
        if not self._in_thinking:
            self.on_thinking_start()
        sys.stdout.write(f"{_DIM}{text}{_RESET}")
        sys.stdout.flush()

    def on_thinking_end(self):
        self._end_thinking()

    def on_tool_start(self, name: str, args: dict | None):
        self._end_text()
        sys.stdout.write(f"\n  {_DIM}╭─{_RESET} {_BOLD}{name}{_RESET}")
        if args:
            preview = json.dumps(args, ensure_ascii=False)
            if len(preview) > 120:
                preview = preview[:120] + "…"
            sys.stdout.write(f"\n  {_DIM}│{_RESET}  {_YELLOW}{preview}{_RESET}")
        sys.stdout.write("\n")
        sys.stdout.flush()

    def on_tool_end(self, name: str, result: str, is_error: bool):
        color = _RED if is_error else _GREEN
        tag = "error" if is_error else f"{len(result):,} chars"
        preview = result.replace("\n", " ")
        if len(preview) > 200:
            preview = preview[:200] + "…"
        sys.stdout.write(f"  {_DIM}│{_RESET}  {color}→ {preview}{_RESET}\n")
        sys.stdout.write(f"  {_DIM}╰─ {tag}{_RESET}\n\n")
        sys.stdout.flush()

    def on_turn(self, iteration: int):
        if iteration > 0:
            self._end_text()
            sys.stdout.write(f"\n{_DIM}── turn {iteration + 1} ──{_RESET}\n\n")

    def on_error(self, message: str):
        self._end_text()
        sys.stderr.write(f"{_RED}{_BOLD}error:{_RESET} {message}\n")

    def on_compaction(self, data: dict):
        self._end_text()
        msgs_before = data.get("messages_before", 0)
        msgs_after = data.get("messages_after", 0)
        tokens_saved = data.get("tokens_saved", 0)
        sys.stdout.write(
            f"{_DIM}── compacted: {msgs_before}→{msgs_after} messages, "
            f"{tokens_saved:,} tokens saved ──{_RESET}\n"
        )
        sys.stdout.flush()

    def on_tool_delta(self, data: dict):
        content = data.get("content", "")
        if content:
            sys.stdout.write(f"  {_DIM}│{_RESET}  {content}")
            sys.stdout.flush()

    def on_loop_end(self, data: dict):
        self._end_text()
        model = data.get("model", "?")
        inp = data.get("input_tokens", 0)
        out = data.get("output_tokens", 0)
        cost = data.get("cost_usd", 0) or 0
        turns = data.get("turns", 0)
        sys.stdout.write(
            f"\n{_DIM}── {model}  │  "
            f"in: {inp:,}  out: {out:,}  │  "
            f"${cost:.4f}  │  "
            f"turns: {turns} ──{_RESET}\n"
        )

    def on_complete(self, data: dict):
        """Deprecated — kept for backward compat."""
        self.on_loop_end(data)

    def flush(self):
        self._end_text()

    @property
    def text(self) -> str:
        return self._accumulated


class JsonStreamPrinter:
    """NDJSON event output (--output-format stream-json)."""

    def __init__(self):
        self._accumulated = ""

    def _emit(self, event: dict):
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def on_text(self, text: str):
        self._accumulated += text
        self._emit({"type": "text", "text": text})

    def on_thinking_start(self):
        self._emit({"type": "thinking_start"})

    def on_thinking_delta(self, text: str):
        self._emit({"type": "thinking_delta", "text": text})

    def on_thinking_end(self):
        self._emit({"type": "thinking_end"})

    def on_tool_start(self, name: str, args: dict | None):
        self._emit({"type": "tool_start", "name": name, "args": args})

    def on_tool_end(self, name: str, result: str, is_error: bool):
        self._emit({"type": "tool_end", "name": name, "result": result, "is_error": is_error})

    def on_tool_delta(self, data: dict):
        self._emit({"type": "tool_delta", **data})

    def on_turn(self, iteration: int):
        self._emit({"type": "turn", "iteration": iteration})

    def on_turn_end(self, data: dict):
        self._emit({"type": "turn_end", **data})

    def on_compaction(self, data: dict):
        self._emit({"type": "compaction", **data})

    def on_error(self, message: str):
        self._emit({"type": "error", "message": message})

    def on_loop_end(self, data: dict):
        self._emit({"type": "loop_end", **data})

    def on_complete(self, data: dict):
        """Deprecated — kept for backward compat."""
        self.on_loop_end(data)

    def flush(self):
        pass

    @property
    def text(self) -> str:
        return self._accumulated


class SilentPrinter:
    """Collects text, outputs nothing until complete (--output-format json)."""

    def __init__(self):
        self._accumulated = ""
        self._complete_data: dict | None = None

    def on_text(self, text: str):
        self._accumulated += text

    def on_thinking_start(self):
        pass

    def on_thinking_delta(self, text: str):
        pass

    def on_thinking_end(self):
        pass

    def on_tool_start(self, name: str, args: dict | None):
        pass

    def on_tool_end(self, name: str, result: str, is_error: bool):
        pass

    def on_tool_delta(self, data: dict):
        pass

    def on_turn(self, iteration: int):
        pass

    def on_turn_end(self, data: dict):
        pass

    def on_compaction(self, data: dict):
        pass

    def on_error(self, message: str):
        self._accumulated = ""
        self._complete_data = {"error": message}

    def on_loop_end(self, data: dict):
        self._complete_data = data

    def on_complete(self, data: dict):
        """Deprecated — kept for backward compat."""
        self.on_loop_end(data)

    def flush(self):
        pass

    def print_result(self, session_id: str):
        result = {
            "text": self._accumulated,
            "session_id": session_id,
        }
        if self._complete_data:
            # Support both old format (nested "usage" dict) and new format (flat fields)
            usage = self._complete_data.get("usage")
            if usage:
                result["usage"] = usage
            else:
                # Build usage from flat fields
                inp = self._complete_data.get("input_tokens")
                out = self._complete_data.get("output_tokens")
                if inp is not None or out is not None:
                    result["usage"] = {
                        "input_tokens": inp or 0,
                        "output_tokens": out or 0,
                        "model": self._complete_data.get("model"),
                    }
            cost = self._complete_data.get("cost_usd")
            if cost is not None:
                result["cost_usd"] = cost
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")

    @property
    def text(self) -> str:
        return self._accumulated


# ---------------------------------------------------------------------------
# run_once — shared across all output modes
# ---------------------------------------------------------------------------

async def run_once(backend, prompt, printer, **kwargs) -> dict | None:
    """Stream one prompt through the backend, dispatch events to printer."""
    try:
        async for event in backend.stream(prompt, **kwargs):
            match event.type:
                case EventType.TEXT_DELTA:
                    printer.on_text(event.data.get("text", ""))
                case EventType.THINKING_START:
                    printer.on_thinking_start()
                case EventType.THINKING_DELTA:
                    printer.on_thinking_delta(event.data.get("text", ""))
                case EventType.THINKING_END:
                    printer.on_thinking_end()
                case EventType.TOOL_START:
                    printer.on_tool_start(
                        event.data.get("name", ""),
                        event.data.get("args"),
                    )
                case EventType.TOOL_END:
                    printer.on_tool_end(
                        event.data.get("name", ""),
                        event.data.get("result", ""),
                        event.data.get("is_error", False),
                    )
                case EventType.TOOL_DELTA:
                    if hasattr(printer, "on_tool_delta"):
                        printer.on_tool_delta(event.data)
                case EventType.TURN_START:
                    printer.on_turn(event.data.get("iteration", 0))
                case EventType.TURN_END:
                    if hasattr(printer, "on_turn_end"):
                        printer.on_turn_end(event.data)
                case EventType.COMPACTION:
                    if hasattr(printer, "on_compaction"):
                        printer.on_compaction(event.data)
                case EventType.ERROR:
                    printer.on_error(event.data.get("message", "Unknown error"))
                    return None
                case EventType.LOOP_END:
                    if hasattr(printer, "on_loop_end"):
                        printer.on_loop_end(event.data)
                    else:
                        printer.on_complete(event.data)
                    return event.data
                case EventType.LOOP_START:
                    pass  # no visible output
    except KeyboardInterrupt:
        printer.flush()
        sys.stderr.write(f"\n{_DIM}interrupted{_RESET}\n")
        return None

    printer.flush()
    return None


# ---------------------------------------------------------------------------
# complete subcommand — one-shot inference (no tools, no session, no agent loop)
# ---------------------------------------------------------------------------

async def complete_once(
    prompt: str,
    *,
    model: str | None,
    provider: str | None,
    mode: str | None,
    system_prompt: str | None,
    system_prompt_file: str | None,
    temperature: float | None,
    max_tokens: int | None,
    response_format: dict | None,
    output_format: str,
    thinking: str | None = None,
    reasoning_effort: str | None = None,
) -> int:
    """Run one ALoop.complete() call and emit the result to stdout.

    Returns the intended process exit code (0 on success, 1 on error).
    """
    from . import get_project_root
    from .config import load_mode, resolve_mode_system_prompt
    from .providers import get_provider, get_default_provider_name
    from .system_prompt import _load_aloop_config
    from .types import InferenceError

    # --- Resolve mode (for model + optional system_prompt default) ---
    mode_model: str | None = None
    mode_system_prompt: str | None = None
    if mode:
        try:
            root = get_project_root()
            aloop_config = _load_aloop_config(root)
            mode_cfg = load_mode(mode, aloop_config)
        except ValueError as e:
            sys.stderr.write(f"error: {e}\n")
            return 1
        mode_model = mode_cfg.get("model")
        mode_system_prompt = resolve_mode_system_prompt(mode_cfg, root)

    # --- Resolve model ---
    # Precedence: --model > --mode.model > ALOOP_MODEL env > config.default_model > error
    resolved_model = (
        model
        or mode_model
        or os.environ.get("ALOOP_MODEL")
        or _config_default_model()
    )
    if not resolved_model:
        sys.stderr.write(
            "error: no model specified. Use --model, --mode, set ALOOP_MODEL,\n"
            "  or set default_model in ~/.aloop/config.json.\n"
        )
        return 1

    # --- Resolve provider ---
    provider_name = provider or get_default_provider_name()
    try:
        provider_cfg = get_provider(provider_name)
    except KeyError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1

    # --- Resolve system prompt ---
    # Precedence: explicit --system-prompt > --system-prompt-file > mode > None.
    # Note: an explicit empty string "" wins — caller wants no system message.
    if system_prompt is not None:
        sys_prompt: str | None = system_prompt
    elif system_prompt_file is not None:
        sp_path = Path(system_prompt_file)
        if not sp_path.exists():
            sys.stderr.write(f"error: system prompt file not found: {sp_path}\n")
            return 1
        sys_prompt = sp_path.read_text(encoding="utf-8")
    elif mode_system_prompt is not None:
        sys_prompt = mode_system_prompt
    else:
        sys_prompt = None

    # --- Build ALoop instance ---
    try:
        aloop_instance = ALoop(model=resolved_model, provider=provider_cfg)
    except Exception as e:
        sys.stderr.write(f"error: {e}\n")
        return 1

    # --- Run completion ---
    try:
        # Mode-level reasoning controls (only consulted when not overridden
        # by explicit --thinking / --reasoning-effort).
        eff_thinking = thinking
        eff_reasoning_effort = reasoning_effort
        if mode and (eff_thinking is None or eff_reasoning_effort is None):
            mode_cfg_local = mode_cfg if mode else {}
            if eff_thinking is None:
                eff_thinking = mode_cfg_local.get("thinking")
            if eff_reasoning_effort is None:
                eff_reasoning_effort = mode_cfg_local.get("reasoning_effort")

        result = await aloop_instance.complete(
            prompt,
            system_prompt=sys_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            thinking=eff_thinking,
            reasoning_effort=eff_reasoning_effort,
        )
    except InferenceError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1
    except Exception as e:
        sys.stderr.write(f"error: {e}\n")
        return 1

    # --- Emit result ---
    if output_format == "json":
        payload = {
            "text": result.text,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": result.cost_usd,
            "model": result.model,
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(result.text)
        if result.text and not result.text.endswith("\n"):
            sys.stdout.write("\n")

    return 0


async def _cmd_complete(args) -> int:
    """Entry point for the `complete` subcommand.

    Handles stdin detection + prompt assembly, then calls complete_once().
    """
    positional = args.prompt
    stdin_is_tty = sys.stdin.isatty()

    stdin_content: str = ""
    if not stdin_is_tty:
        stdin_content = sys.stdin.read()

    # --- Assemble prompt ---
    if positional and not stdin_is_tty and stdin_content.strip():
        # Combine leading instruction + piped body
        prompt = f"{positional}\n\n{stdin_content.rstrip()}"
    elif positional:
        prompt = positional
    elif not stdin_is_tty and stdin_content.strip():
        prompt = stdin_content.rstrip()
    else:
        sys.stderr.write(
            "error: no prompt provided (give a positional arg or pipe stdin)\n"
        )
        return 1

    # --- Resolve response_format ---
    response_format: dict | None = None
    if args.response_format:
        try:
            response_format = json.loads(args.response_format)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"error: --response-format is not valid JSON: {e}\n")
            return 1
    elif args.json_mode:
        response_format = {"type": "json_object"}

    return await complete_once(
        prompt,
        model=args.model,
        provider=args.provider,
        mode=args.mode,
        system_prompt=args.system_prompt_override,
        system_prompt_file=args.system_prompt_file,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        response_format=response_format,
        output_format=args.output_format,
        thinking=getattr(args, "thinking", None),
        reasoning_effort=getattr(args, "reasoning_effort", None),
    )


# ---------------------------------------------------------------------------
# Self-update
# ---------------------------------------------------------------------------

GIT_URL = "git+https://github.com/zackham/aloop.git"


def _detect_install_method() -> str:
    import shutil
    import subprocess

    exe = shutil.which("aloop") or ""
    if "/uv/" in exe:
        return "uv"
    if "/pipx/" in exe:
        return "pipx"

    for tool, cmd in [("uv", ["uv", "tool", "list"]), ("pipx", ["pipx", "list", "--short"])]:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if out.returncode == 0 and "aloop" in out.stdout:
                return tool
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    return "pip"


def _run_update() -> int:
    import subprocess
    import importlib.metadata

    try:
        dist = importlib.metadata.distribution("aloop")
        direct_url_text = dist.read_text("direct_url.json")
        if direct_url_text:
            data = json.loads(direct_url_text)
            if data.get("dir_info", {}).get("editable"):
                print(f"{_YELLOW}Running from editable install — 'aloop update' is disabled.{_RESET}")
                print(f"To update, pull the latest source: {_DIM}cd <repo> && git pull{_RESET}")
                return 0
    except Exception:
        pass

    from . import __version__
    method = _detect_install_method()

    upgrade_cmds = {
        "uv": ["uv", "tool", "install", "--force", GIT_URL],
        "pipx": ["pipx", "install", "--force", GIT_URL],
        "pip": [sys.executable, "-m", "pip", "install", "--upgrade", GIT_URL],
    }

    cmd = upgrade_cmds[method]
    print(f"Upgrading via {method}...")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(f"{_RED}Upgrade failed (exit {result.returncode}){_RESET}\n")
        stderr = result.stderr.strip()
        if stderr:
            sys.stderr.write(f"{stderr}\n")
        return 1

    ver_result = subprocess.run(
        ["aloop", "--version"], capture_output=True, text=True
    )
    new_version = ver_result.stdout.strip() if ver_result.returncode == 0 else "unknown"

    if new_version == __version__:
        print(f"{_GREEN}Already at latest version ({__version__}){_RESET}")
    else:
        print(f"{_GREEN}Updated: {__version__} → {new_version}{_RESET}")

    return 0


# ---------------------------------------------------------------------------
# Resolve model and API key
# ---------------------------------------------------------------------------

def _config_default_model() -> str | None:
    """Return `default_model` from merged aloop config, or None.

    Reads the same merged global (~/.aloop/config.json) + project (.aloop/config.json)
    view that mode resolution uses. Project wins over global on key collision.
    """
    try:
        from .system_prompt import _load_aloop_config
        from . import get_project_root
        config = _load_aloop_config(get_project_root())
    except Exception:
        return None
    value = config.get("default_model")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _resolve_model(args) -> str:
    # Precedence: --model > ALOOP_MODEL env > config.default_model > error
    model = args.model
    if model is None:
        model = os.environ.get("ALOOP_MODEL")
    if model is None:
        model = _config_default_model()
    if model is None:
        sys.stderr.write(
            "error: no model specified. Use --model, set ALOOP_MODEL, or set\n"
            "  default_model in ~/.aloop/config.json.\n"
            "  Example: aloop --model x-ai/grok-4.1-fast \"your prompt\"\n"
            "  Any OpenRouter model ID works: https://openrouter.ai/models\n"
        )
        sys.exit(1)
    return model


def _resolve_api_key(provider) -> str:
    """Resolve API key: provider env var → credentials file → interactive prompt."""
    from .providers import ProviderConfig

    # 1. Provider-specific env var
    api_key = ""
    if provider.env_key:
        api_key = os.environ.get(provider.env_key, "")

    # 2. Generic env var
    if not api_key:
        api_key = os.environ.get("ALOOP_API_KEY", "")

    # 3. Credentials file (supports JSONC comments)
    if not api_key:
        cred_file = Path.home() / ".aloop" / "credentials.json"
        if cred_file.exists():
            from .utils import load_jsonc
            creds = load_jsonc(cred_file)
            api_key = creds.get(provider.env_key, creds.get("api_key", ""))

    # 4. No key needed (e.g. Ollama)
    if not provider.env_key:
        return api_key or "no-key-needed"

    # 5. Interactive prompt
    if not api_key:
        if not sys.stdin.isatty():
            sys.stderr.write(f"error: no API key. Set {provider.env_key} or ALOOP_API_KEY.\n")
            sys.exit(1)
        print(f"No API key found for {provider.name}.\n")
        if provider.env_key:
            print(f"  {_DIM}Set {provider.env_key} or paste below:{_RESET}\n")
        try:
            api_key = input("Paste your API key: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
        if not api_key:
            sys.stderr.write("error: no API key provided\n")
            sys.exit(1)
        # Save for next time
        cred_file = Path.home() / ".aloop" / "credentials.json"
        cred_file.parent.mkdir(parents=True, exist_ok=True)
        creds = {}
        if cred_file.exists():
            try:
                from .utils import load_jsonc
                creds = load_jsonc(cred_file)
            except (OSError, json.JSONDecodeError):
                pass
        creds[provider.env_key] = api_key
        cred_file.write_text(json.dumps(creds))
        cred_file.chmod(0o600)
        print(f"\n{_GREEN}Saved to {cred_file}{_RESET}\n")

    return api_key


# ---------------------------------------------------------------------------
# Register with acpx
# ---------------------------------------------------------------------------

_ACPX_CONFIG = Path.home() / ".acpx" / "config.json"


def _run_register() -> int:
    """Register aloop as an ACP agent with acpx."""
    import shutil

    if not shutil.which("acpx"):
        print(f"{_YELLOW}acpx not found.{_RESET}")
        print(f"Install it: {_DIM}npm install -g acpx{_RESET}")
        return 1

    # Load or create acpx config
    config: dict = {}
    if _ACPX_CONFIG.exists():
        try:
            from .utils import load_jsonc
            config = load_jsonc(_ACPX_CONFIG)
        except (OSError, json.JSONDecodeError):
            pass

    agents = config.get("agents", {})

    expected = {"command": "aloop serve"}

    # Check if already registered correctly
    if agents.get("aloop") == expected:
        print(f"{_GREEN}aloop is already registered with acpx.{_RESET}")
        print(f"\n  {_DIM}acpx aloop \"your prompt\"{_RESET}")
        return 0

    # Register
    agents["aloop"] = expected
    config["agents"] = agents

    _ACPX_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    _ACPX_CONFIG.write_text(json.dumps(config, indent=2))

    print(f"{_GREEN}Registered aloop with acpx.{_RESET}")
    print(f"\n  {_DIM}acpx aloop \"your prompt\"{_RESET}")
    print(f"  {_DIM}# In Stepwise flows: agent: aloop{_RESET}")

    return 0


# ---------------------------------------------------------------------------
# Provider validation
# ---------------------------------------------------------------------------

async def _run_validate_provider(args) -> int:
    """Test a provider's API compatibility with aloop."""
    from .providers import get_provider, get_providers

    provider_name = args.provider
    model = args.model

    try:
        provider = get_provider(provider_name)
    except KeyError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1

    # Resolve API key
    api_key = ""
    if provider.env_key:
        api_key = os.environ.get(provider.env_key, "")
    if not api_key:
        api_key = os.environ.get("ALOOP_API_KEY", "")
    if not api_key and provider.env_key:
        sys.stderr.write(f"error: set {provider.env_key} or ALOOP_API_KEY\n")
        return 1

    print(f"Validating {_BOLD}{provider.name}{_RESET} with model {_BOLD}{model}{_RESET}\n")

    backend = ALoop(model=model, api_key=api_key, provider=provider)
    tests_passed = 0
    tests_failed = 0

    async def _run_test(name: str, prompt: str, expect_tools: bool = False):
        nonlocal tests_passed, tests_failed
        sys.stdout.write(f"  {name:40s} ")
        sys.stdout.flush()

        got_text = False
        got_tool = False
        got_complete = False
        error_msg = ""

        try:
            tools = None
            if expect_tools:
                from .tools_base import ToolDef
                tools = [ToolDef(
                    name="get_weather",
                    description="Get current weather for a city",
                    parameters={
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                    execute=None,
                )]

            async for event in backend.stream(prompt, tools=tools):
                if event.type == EventType.TEXT_DELTA:
                    got_text = True
                elif event.type == EventType.TOOL_START:
                    got_tool = True
                elif event.type == EventType.LOOP_END:
                    got_complete = True
                elif event.type == EventType.ERROR:
                    error_msg = event.data.get("message", "")
        except Exception as e:
            error_msg = str(e)

        if error_msg:
            print(f"{_RED}FAIL{_RESET}  {error_msg[:80]}")
            tests_failed += 1
        elif expect_tools and not got_tool:
            print(f"{_YELLOW}WARN{_RESET}  no tool call (model may not support tools)")
            tests_passed += 1  # not a hard failure
        elif not got_complete:
            print(f"{_RED}FAIL{_RESET}  no completion event")
            tests_failed += 1
        elif not got_text and not got_tool:
            print(f"{_RED}FAIL{_RESET}  no text or tool output")
            tests_failed += 1
        else:
            print(f"{_GREEN}PASS{_RESET}")
            tests_passed += 1

    await _run_test("Basic completion", "Say 'hello' and nothing else.")
    await _run_test("Streaming", "Count from 1 to 5, one number per line.")
    await _run_test("Tool calling", "What is the weather in Tokyo?", expect_tools=True)
    await _run_test("Multi-turn context",
                    "Remember: the secret word is 'banana'. What is the secret word?")

    print(f"\n{tests_passed} passed, {tests_failed} failed")

    if tests_failed == 0:
        print(f"\n{_GREEN}{provider.name} is fully compatible.{_RESET}")
    else:
        print(f"\n{_YELLOW}Some tests failed. The provider may still work for basic prompts.{_RESET}")

    return 0 if tests_failed == 0 else 1


# ---------------------------------------------------------------------------
# Config show
# ---------------------------------------------------------------------------

def _run_config_show() -> int:
    """Show resolved configuration."""
    from . import get_project_root
    from .system_prompt import (
        _load_aloop_config, _load_template, _find_instruction_file,
        _find_skills_dirs, _load_json_file, OVERRIDABLE_SECTIONS,
        INSTRUCTION_CANDIDATES,
    )
    from .hooks import get_discovered_hooks
    from .tools.skills import get_skills_by_source
    from .providers import get_default_provider_name

    root = get_project_root()
    config = _load_aloop_config(root)

    print(f"{_BOLD}aloop configuration{_RESET}\n")

    # Project root
    print(f"  {_DIM}project root:{_RESET}    {root}")

    # Config files (global + project)
    global_config_path = Path.home() / ".aloop" / "config.json"
    project_config_path = root / ".aloop" / "config.json"
    if global_config_path.exists():
        print(f"  {_DIM}global config:{_RESET}   {global_config_path}")
    else:
        print(f"  {_DIM}global config:{_RESET}   {_DIM}(none){_RESET}")
    if project_config_path.exists():
        print(f"  {_DIM}project config:{_RESET}  {project_config_path}")
    else:
        print(f"  {_DIM}project config:{_RESET}  {_DIM}(none){_RESET}")

    # Instruction file (unified discovery chain)
    found_instruction, skipped = _find_instruction_file(root)

    if found_instruction:
        # Build explanation string
        found_name = str(found_instruction.relative_to(root))
        not_found = [
            c for c in INSTRUCTION_CANDIDATES
            if not (root / c).exists()
        ]
        info_parts = []
        if not_found:
            # Only show candidates that were checked before the found one
            checked_before = []
            for c in INSTRUCTION_CANDIDATES:
                if (root / c) == found_instruction:
                    break
                if c in not_found:
                    checked_before.append(c)
            if checked_before:
                info_parts.append(f"{', '.join(checked_before)} not found")
        if skipped:
            skip_names = [str(s.relative_to(root)) for s in skipped]
            info_parts.append(f"{', '.join(skip_names)} also exist but lower priority")

        print(f"  {_DIM}instructions:{_RESET}    {found_instruction}")
        if info_parts:
            print(f"                     {_DIM}({'; '.join(info_parts)}){_RESET}")
    else:
        print(f"  {_DIM}instructions:{_RESET}    {_DIM}(none){_RESET}")

    # System prompt mode
    template = _load_template(root, config)
    if template:
        sp_key = config.get("system_prompt", "")
        if isinstance(sp_key, str) and sp_key.startswith("file:"):
            print(f"  {_DIM}prompt mode:{_RESET}     template ({sp_key})")
        else:
            print(f"  {_DIM}prompt mode:{_RESET}     template (inline)")
    else:
        print(f"  {_DIM}prompt mode:{_RESET}     section (default)")

    # Section overrides
    overrides = config.get("sections", {})
    if isinstance(overrides, dict) and overrides:
        print(f"\n  {_DIM}section overrides:{_RESET}")
        for name in OVERRIDABLE_SECTIONS:
            val = overrides.get(name)
            if val is False:
                print(f"    {name}: {_RED}omitted{_RESET}")
            elif isinstance(val, str):
                preview = val[:60].replace("\n", " ")
                print(f"    {name}: {_GREEN}custom{_RESET} ({preview}...)")

    # Skills (merged across directories)
    skills_by_source = get_skills_by_source()
    disabled_skills = config.get("disabled_skills", [])
    if skills_by_source:
        print(f"\n  {_DIM}skills:{_RESET}")
        for source, names in skills_by_source.items():
            # Determine if global or project
            if str(Path.home()) in source and ".aloop" in source:
                scope = "global"
            else:
                scope = "project"
            print(f"    {_DIM}[{scope}]{_RESET} {source}")
            print(f"      {', '.join(sorted(names))}")
    else:
        print(f"\n  {_DIM}skills:{_RESET}          {_DIM}(none){_RESET}")

    if disabled_skills:
        print(f"  {_DIM}disabled skills:{_RESET} {', '.join(disabled_skills)}")

    # Hooks (global + project)
    hooks_info = get_discovered_hooks(root)
    disabled_hooks = config.get("disabled_hooks", [])
    has_hooks = hooks_info["global"] or hooks_info["project"]
    if has_hooks:
        print(f"\n  {_DIM}hooks:{_RESET}")
        if hooks_info["global"]:
            global_dir = Path.home() / ".aloop" / "hooks"
            print(f"    {_DIM}[global]{_RESET} {global_dir}")
            print(f"      {', '.join(hooks_info['global'])}")
        if hooks_info["project"]:
            project_dir = root / ".aloop" / "hooks"
            print(f"    {_DIM}[project]{_RESET} {project_dir}")
            print(f"      {', '.join(hooks_info['project'])}")
    else:
        print(f"\n  {_DIM}hooks:{_RESET}           {_DIM}(none){_RESET}")

    if disabled_hooks:
        print(f"  {_DIM}disabled hooks:{_RESET}  {', '.join(disabled_hooks)}")

    # Merged config summary
    if config:
        print(f"\n  {_DIM}merged config:{_RESET}")
        # Show key config values (excluding verbose sections)
        for key in sorted(config.keys()):
            if key in ("sections",):
                continue  # already shown above
            val = config[key]
            if isinstance(val, str) and len(val) > 80:
                val = val[:77] + "..."
            print(f"    {key}: {val}")

    # Provider / model — match runtime precedence: env var > config default.
    provider_name = config.get("default_provider") or config.get("provider") or get_default_provider_name()
    model = os.environ.get("ALOOP_MODEL") or config.get("default_model") or "(not set)"
    print(f"\n  {_DIM}provider:{_RESET}        {provider_name}")
    print(f"  {_DIM}model:{_RESET}           {model}")

    return 0


def _run_config_validate() -> int:
    """Validate config files (JSONC parsing + subagent config consistency)."""
    import json as _json
    from .config import validate_subagent_config
    from .system_prompt import _load_aloop_config
    from .utils import strip_json_comments
    from . import get_project_root

    root = get_project_root()
    files_to_check = [
        ("global config", Path.home() / ".aloop" / "config.json"),
        ("project config", root / ".aloop" / "config.json"),
        ("global compaction", Path.home() / ".aloop" / "compaction.json"),
        ("models", Path.home() / ".aloop" / "models.json"),
        ("providers", Path.home() / ".aloop" / "providers.json"),
        ("credentials", Path.home() / ".aloop" / "credentials.json"),
    ]

    errors = 0
    for label, path in files_to_check:
        if not path.exists():
            print(f"  {_DIM}{label}: {path} (not found){_RESET}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
            stripped = strip_json_comments(text)
            _json.loads(stripped)
            print(f"  {_GREEN}{label}: {path} OK{_RESET}")
        except _json.JSONDecodeError as e:
            print(f"  {_RED}{label}: {path} INVALID{_RESET}")
            print(f"    {e}")
            errors += 1
        except OSError as e:
            print(f"  {_RED}{label}: {path} ERROR{_RESET}")
            print(f"    {e}")
            errors += 1

    # Subagent config validation — checks spawnable_modes references and
    # subagent_eligible consistency. Runs on the merged project config.
    try:
        merged_config = _load_aloop_config(root)
    except Exception:
        merged_config = {}
    sub_errors = validate_subagent_config(merged_config)
    if sub_errors:
        print(f"\n{_RED}Subagent config errors:{_RESET}")
        for err in sub_errors:
            print(f"  {_RED}-{_RESET} {err}")
        errors += len(sub_errors)
    else:
        print(f"  {_GREEN}subagent config: OK{_RESET}")

    if errors:
        print(f"\n{_RED}{errors} config error(s).{_RESET}")
        return 1
    else:
        print(f"\n{_GREEN}All config files valid.{_RESET}")
        return 0


# ---------------------------------------------------------------------------
# Init scaffold
# ---------------------------------------------------------------------------

_INIT_CONFIG_TEMPLATE = """\
{
  // System prompt: use "file:ALOOP-PROMPT.md" for template mode
  // "system_prompt": "file:ALOOP-PROMPT.md",

  // Section overrides (only used when system_prompt is not set)
  // "sections": {
  //   "preamble": false,
  //   "identity": "You are a helpful coding agent."
  // },

  // Modes for different workflows
  // "modes": {
  //   "review": {
  //     "system_prompt": "You are a code reviewer.",
  //     "tools": ["read_file", "bash"]
  //   }
  // },

  // Provider default (openrouter, openai, anthropic, google, groq, etc.)
  // "provider": "openrouter"
}
"""

_INIT_HOOKS_TEMPLATE = """\
\"\"\"aloop hooks — extend the agent loop without modifying aloop source.

Register hooks by decorating functions with @hook(\"hook_name\").

Available hooks:
  register_tools    — return a list of ToolDef to add custom tools
  before_tool       — called before each tool execution (can block/modify)
  after_tool        — called after each tool execution (can transform results)
  gather_context    — inject dynamic context into the system prompt

See: https://github.com/zackham/aloop/blob/main/docs/HOOKS.md
\"\"\"

from aloop_hooks import hook
# from aloop import ToolDef, ToolResult

# Example: register a custom tool
# @hook("register_tools")
# def my_tools():
#     async def _hello(name: str) -> ToolResult:
#         return ToolResult(content=f"Hello, {name}!")
#     return [ToolDef(
#         name="hello",
#         description="Say hello",
#         parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
#         execute=_hello,
#     )]
"""


def _run_init() -> int:
    """Scaffold .aloop/ directory in the current working directory."""
    root = Path.cwd()
    aloop_dir = root / ".aloop"

    if aloop_dir.exists():
        print(f"{_YELLOW}.aloop/ already exists in {root}{_RESET}")
        # Still create missing subdirectories
        created = []
    else:
        aloop_dir.mkdir(parents=True)
        created = [".aloop/"]

    # config.json
    config_path = aloop_dir / "config.json"
    if not config_path.exists():
        config_path.write_text(_INIT_CONFIG_TEMPLATE)
        created.append(".aloop/config.json")

    # hooks/
    hooks_dir = aloop_dir / "hooks"
    if not hooks_dir.exists():
        hooks_dir.mkdir()
        created.append(".aloop/hooks/")

    # hooks/__init__.py
    hooks_init = hooks_dir / "__init__.py"
    if not hooks_init.exists():
        hooks_init.write_text(_INIT_HOOKS_TEMPLATE)
        created.append(".aloop/hooks/__init__.py")

    # skills/
    skills_dir = aloop_dir / "skills"
    if not skills_dir.exists():
        skills_dir.mkdir()
        created.append(".aloop/skills/")

    if created:
        print(f"{_GREEN}Scaffolded .aloop/ in {root}{_RESET}")
        for f in created:
            print(f"  {_DIM}+ {f}{_RESET}")
    else:
        print(f"All files already exist in {aloop_dir}")

    return 0


# ---------------------------------------------------------------------------
# Sessions management
# ---------------------------------------------------------------------------

def _run_sessions(args) -> int:
    """Handle the 'sessions' subcommand."""
    from .session import AgentSession, list_sessions, gc_sessions

    action = getattr(args, "sessions_action", None)

    if action == "list":
        sessions = list_sessions()
        if not sessions:
            print(f"{_DIM}No sessions found.{_RESET}")
            return 0
        print(f"{_BOLD}{'SESSION ID':<20s} {'MESSAGES':>8s} {'FORK FROM':<20s} {'FORK TURN':<14s} {'LAST ACTIVE'}{_RESET}")
        for s in sessions:
            sid = s.get("session_id", "?")
            count = s.get("message_count", 0)
            fork = s.get("fork_from") or ""
            fturn = s.get("fork_turn_id") or ""
            last_active = s.get("last_active")
            if last_active:
                import datetime
                ts = datetime.datetime.fromtimestamp(last_active).strftime("%Y-%m-%d %H:%M")
            else:
                ts = "?"
            print(f"  {sid:<20s} {count:>8d} {fork:<20s} {fturn:<14s} {ts}")
        print(f"\n{_DIM}{len(sessions)} session(s){_RESET}")
        return 0

    elif action == "info":
        sid = args.session_id
        session = AgentSession.load(sid)
        if session is None:
            sys.stderr.write(f"error: session {sid!r} not found\n")
            return 1
        print(f"{_BOLD}Session: {session.session_id}{_RESET}")
        print(f"  {_DIM}fork_from:{_RESET}    {session.fork_from or '(none)'}")
        print(f"  {_DIM}fork_turn_id:{_RESET} {session.fork_turn_id or '(none)'}")
        print(f"  {_DIM}fork_depth:{_RESET}   {session.fork_depth()}")
        children = session.children()
        print(f"  {_DIM}children:{_RESET}     {', '.join(children) if children else '(none)'}")
        print(f"  {_DIM}messages:{_RESET}     {len(session.messages)} stored")
        if session.spawn_metadata:
            sm = session.spawn_metadata
            print(f"  {_DIM}spawn:{_RESET}        {sm.get('kind', '?')}")
            print(f"  {_DIM}parent:{_RESET}       {sm.get('parent_session_id') or '(none)'}")
            print(f"  {_DIM}parent_turn:{_RESET}  {sm.get('parent_turn_id') or '(none)'}")
            print(f"  {_DIM}spawning_mode:{_RESET} {sm.get('spawning_mode') or '(none)'}")
            print(f"  {_DIM}child_mode:{_RESET}    {sm.get('child_mode') or '(inherited)'}")
        resolved = session.resolve_messages()
        print(f"  {_DIM}resolved:{_RESET}     {len(resolved)} total")
        if resolved:
            print(f"\n{_BOLD}Messages:{_RESET}")
            for i, msg in enumerate(resolved):
                role = msg.get("role", "?")
                turn_id = msg.get("turn_id", "")
                content = str(msg.get("content", ""))
                preview = content.replace("\n", " ")[:80]
                if len(content) > 80:
                    preview += "..."
                print(f"  {i:3d}  {_DIM}[{turn_id or '-':>12s}]{_RESET} {role:<10s} {preview}")
        return 0

    elif action == "gc":
        max_age = args.max_age
        deleted = gc_sessions(max_age_seconds=max_age)
        if deleted:
            print(f"{_GREEN}Deleted {len(deleted)} session(s):{_RESET}")
            for sid in deleted:
                print(f"  {sid}")
        else:
            print(f"{_DIM}No expired sessions to clean up.{_RESET}")
        return 0

    elif action == "materialize":
        sid = args.session_id
        session = AgentSession.load(sid)
        if session is None:
            sys.stderr.write(f"error: session {sid!r} not found\n")
            return 1
        if session.fork_from is None:
            print(f"{_DIM}Session {sid} is not forked — nothing to materialize.{_RESET}")
            return 0
        before_count = len(session.messages)
        session.materialize()
        print(f"{_GREEN}Materialized session {sid}{_RESET}")
        print(f"  {_DIM}messages:{_RESET} {before_count} -> {len(session.messages)}")
        print(f"  {_DIM}fork_from:{_RESET} cleared")
        return 0

    elif action == "rebuild-index":
        from .session import _rebuild_fork_index
        index = _rebuild_fork_index()
        total = sum(len(v) for v in index.values())
        print(f"Rebuilt fork index: {len(index)} parent(s), {total} child(ren)")
        return 0

    else:
        sys.stderr.write("usage: aloop sessions {list,info,gc,materialize,rebuild-index}\n")
        return 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    args = parse_args()

    # --version on the root parser
    if args.version:
        from . import __version__
        print(__version__)
        return

    subcmd = args.subcommand

    # --- version ---
    if subcmd == "version":
        from . import __version__
        print(__version__)
        return

    # --- update ---
    if subcmd == "update":
        sys.exit(_run_update())

    # --- register-acpx ---
    if subcmd == "register-acpx":
        sys.exit(_run_register())

    # --- init ---
    if subcmd == "init":
        sys.exit(_run_init())

    # --- providers ---
    if subcmd == "providers":
        action = getattr(args, "providers_action", None)
        if action == "list":
            from .providers import get_providers
            providers = get_providers()
            for key, p in sorted(providers.items()):
                status = f"{_GREEN}tested{_RESET}" if p.status == "tested" else f"{_DIM}community{_RESET}"
                print(f"  {key:20s}  {p.name:30s}  [{status}]")
                if p.notes:
                    print(f"  {' ':20s}  {_DIM}{p.notes}{_RESET}")
            return
        elif action == "validate":
            sys.exit(await _run_validate_provider(args))
        else:
            sys.stderr.write("usage: aloop providers {list,validate}\n")
            sys.exit(1)

    # --- config ---
    if subcmd == "config":
        action = getattr(args, "config_action", None)
        if action == "show":
            sys.exit(_run_config_show())
        elif action == "validate":
            sys.exit(_run_config_validate())
        else:
            sys.stderr.write("usage: aloop config {show,validate}\n")
            sys.exit(1)

    # --- serve ---
    if subcmd == "serve":
        from .acp import serve_acp
        await serve_acp(model=args.model)
        return

    # --- system-prompt ---
    if subcmd == "system-prompt":
        from .system_prompt import _load_aloop_config, _load_template
        from . import get_project_root
        root = get_project_root()
        config = _load_aloop_config(root)
        template = _load_template(root, config)
        if template:
            if args.rendered:
                print(build_system_prompt())
            else:
                print(template)
        else:
            print(build_system_prompt())
        print(f"\n{_DIM}---{_RESET}")
        print(f"{_DIM}Available variables: {{{{tools}}}}, {{{{skills}}}}, {{{{agents_md}}}}{_RESET}")
        return

    # --- sessions ---
    if subcmd == "sessions":
        sys.exit(_run_sessions(args))
        return

    # --- complete (one-shot, no tools/session/agent loop) ---
    if subcmd == "complete":
        exit_code = await _cmd_complete(args)
        sys.exit(exit_code)

    # --- run (default) ---
    if subcmd == "run":
        await _run_prompt(args)
        return

    # Fallback — shouldn't happen with proper subparser setup
    sys.stderr.write("error: no subcommand specified. Run 'aloop --help' for usage.\n")
    sys.exit(1)


async def _run_prompt(args):
    """Handle the 'run' subcommand — the main prompt execution path."""

    # --- Resolve session ID ---
    if args.continue_last:
        state = _load_state()
        session_id = state.get("last_session")
        if not session_id:
            sys.stderr.write("error: no previous session to continue\n")
            sys.exit(1)
    elif args.resume:
        session_id = args.resume
    elif args.session:
        session_id = args.session
    else:
        # Auto-create a session for every invocation
        session_id = uuid.uuid4().hex[:12]

    # --- Resolve prompt ---
    prompt = args.prompt
    piped = not sys.stdin.isatty()
    if not prompt and piped:
        prompt = sys.stdin.read().strip()

    # -p is implied by pipe input or structured output
    print_mode = args.print_mode or piped or args.output_format != "text"

    # No prompt: if interactive, go straight to REPL. If -p, error.
    if not prompt and print_mode:
        sys.stderr.write("error: no prompt provided\n")
        sys.exit(1)

    # --- Resolve provider, model & key ---
    from .providers import get_provider, get_default_provider_name
    provider_name = args.provider or get_default_provider_name()
    try:
        provider = get_provider(provider_name)
    except KeyError as e:
        sys.stderr.write(f"error: {e}\n")
        sys.exit(1)

    model = _resolve_model(args)
    api_key = _resolve_api_key(provider)

    # --- Build backend ---
    try:
        backend = ALoop(
            model=model,
            api_key=api_key,
            provider=provider,
            max_iterations=args.max_iterations,
        )
    except Exception as e:
        sys.stderr.write(f"error: {e}\n")
        sys.exit(1)

    # --- Build stream kwargs ---
    stream_kw: dict = {"session_id": session_id}

    # --- Mode ---
    if args.mode:
        stream_kw["mode"] = args.mode

    from .tools import ANALYSIS_TOOLS
    from .tools.skills import load_skill_tool
    default_tools = list(ANALYSIS_TOOLS)
    if not any(t.name == "load_skill" for t in default_tools):
        default_tools = default_tools + [load_skill_tool]

    if args.tools:
        # Explicit --tools: filter from defaults, pass as explicit override.
        # This wins over any mode tool list.
        tool_names = {t.strip() for t in args.tools.split(",")}
        filtered = [t for t in default_tools if t.name in tool_names]
        unknown = tool_names - {t.name for t in filtered}
        if unknown:
            sys.stderr.write(f"warning: unknown tools: {', '.join(sorted(unknown))}\n")
        stream_kw["tools"] = filtered
        tools = filtered
    elif args.mode:
        # Mode is set without explicit --tools: let the mode's tool list take
        # effect. Passing tools= to stream() would override the mode (since
        # explicit tools= is treated as a full replacement). Leave it unset.
        tools = default_tools  # used below for system prompt building only if mode doesn't set one
    else:
        # No mode, no explicit --tools: use defaults.
        stream_kw["tools"] = default_tools
        tools = default_tools

    # --- Resolve system prompt ---
    # When a mode is specified without explicit --system-prompt/--system-prompt-file,
    # let the mode's system prompt take effect (stream() handles this).
    if args.system_prompt_override:
        stream_kw["system_prompt"] = args.system_prompt_override
    elif args.system_prompt_file:
        sp_path = Path(args.system_prompt_file)
        if not sp_path.exists():
            sys.stderr.write(f"error: system prompt file not found: {sp_path}\n")
            sys.exit(1)
        stream_kw["system_prompt"] = sp_path.read_text(encoding="utf-8")
    elif not args.mode:
        # No mode — use the default built system prompt
        stream_kw["system_prompt"] = build_system_prompt(tools=tools)

    if args.no_context:
        stream_kw["inject_context"] = False

    # --- Reasoning controls (for thinking-capable models e.g. DeepSeek V4) ---
    if getattr(args, "thinking", None):
        stream_kw["thinking"] = args.thinking
    if getattr(args, "reasoning_effort", None):
        stream_kw["reasoning_effort"] = args.reasoning_effort

    # --- Save as last session ---
    _save_state({"last_session": session_id})

    # --- Pick output adapter ---
    if args.output_format == "stream-json":
        printer = JsonStreamPrinter()
    elif args.output_format == "json":
        printer = SilentPrinter()
    else:
        printer = StreamPrinter()

    # --- Run first prompt (if we have one) ---
    if prompt:
        await run_once(backend, prompt, printer, **stream_kw)

    # --- Print mode: output and exit ---
    if print_mode:
        if isinstance(printer, SilentPrinter):
            printer.print_result(session_id)
        elif isinstance(printer, JsonStreamPrinter):
            pass  # session ID already in the complete event
        else:
            sys.stderr.write(f"\n{_DIM}session: {session_id}{_RESET}\n")
        return

    # --- Interactive REPL ---
    if not sys.stdin.isatty():
        return

    # Enable readline for line editing (ctrl-a/e/w, history, etc.)
    try:
        import readline  # noqa: F401
    except ImportError:
        pass

    if not prompt:
        # No initial prompt — show welcome
        sys.stdout.write(f"{_DIM}session: {session_id}{_RESET}\n\n")

    while True:
        try:
            next_prompt = input(f"\n{_CYAN}>>>{_RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{_DIM}session: {session_id}{_RESET}")
            break

        if not next_prompt:
            continue

        if next_prompt.lower() in ("exit", "quit", "/quit", "/exit"):
            print(f"{_DIM}session: {session_id}{_RESET}")
            break

        await run_once(backend, next_prompt, printer, **stream_kw)

    return


def main_sync():
    """Sync entry point for the `aloop` CLI command."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.stderr.write(f"\n{_DIM}interrupted{_RESET}\n")
        sys.exit(130)


if __name__ == "__main__":
    main_sync()
