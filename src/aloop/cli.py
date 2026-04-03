"""aloop CLI — agent loop with tool access, skills, and hooks.

Usage:
    aloop "prompt"                        # interactive session (auto-created)
    aloop -p "prompt"                     # one-shot, print and exit
    aloop -c                              # continue last session
    aloop --resume SESSION_ID "prompt"    # resume a specific session
    aloop --model x-ai/grok-4.1-fast "prompt"
    echo "prompt" | aloop                 # pipe (one-shot)
    aloop --output-format stream-json -p "prompt"  # NDJSON events
    aloop update                          # self-update
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

from .agent_backend import AgentLoopBackend
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


def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _save_state(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Agent loop with tool access, skills, and hooks",
        prog="aloop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "commands:\n"
            "  aloop update               Self-update to latest version\n"
            "  aloop register-acpx        Register aloop with acpx for ACP integration\n"
            "  aloop list-providers        List available API providers\n"
            "  aloop validate-provider     Test a provider's API compatibility\n"
            "  aloop system-prompt        Show the system prompt template\n"
            "  aloop system-prompt --rendered  Show fully interpolated prompt"
        ),
    )
    parser.add_argument("prompt", nargs="?",
                        help="Prompt text, or a command (see below)")
    parser.add_argument("--version", action="store_true", help="Show version and exit")
    parser.add_argument("--model", "-m", default=None,
                        help="Model ID (e.g. x-ai/grok-4.1-fast)")
    parser.add_argument("--provider", default=None,
                        help="API provider (default: openrouter). See aloop list-providers")
    parser.add_argument("-p", action="store_true", dest="print_mode",
                        help="One-shot: print response and exit (no REPL)")
    parser.add_argument("-c", "--continue", action="store_true", dest="continue_last",
                        help="Continue last session")
    parser.add_argument("--resume", metavar="SESSION_ID",
                        help="Resume a specific session by ID")
    parser.add_argument("-s", "--session", default=None,
                        help="Use a named session (e.g. -s refactor) instead of auto-generated ID")
    parser.add_argument("--output-format", "-o", default="text",
                        choices=["text", "json", "stream-json"],
                        help="Output format (default: text)")
    parser.add_argument("--tools", default=None, help="Comma-separated tool names")
    parser.add_argument("--no-context", action="store_true", help="Skip context injection")
    parser.add_argument("--max-iterations", type=int, default=50)
    parser.add_argument("--list-models", action="store_true", help="List registered model aliases")
    parser.add_argument("--acp", action="store_true", help="Run as ACP server (stdio)")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Output adapters
# ---------------------------------------------------------------------------

class StreamPrinter:
    """ANSI-formatted terminal output (--output-format text)."""

    def __init__(self):
        self._in_text = False
        self._accumulated = ""

    def _end_text(self):
        if self._in_text:
            sys.stdout.write("\n")
            self._in_text = False

    def on_text(self, text: str):
        sys.stdout.write(text)
        sys.stdout.flush()
        self._in_text = bool(text)
        self._accumulated += text

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

    def on_complete(self, data: dict):
        self._end_text()
        usage = data.get("usage") or {}
        model = usage.get("model", "?")
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        cost = usage.get("cost_usd", 0)
        sys.stdout.write(
            f"\n{_DIM}── {model}  │  "
            f"in: {inp:,}  out: {out:,}  │  "
            f"${cost:.4f} ──{_RESET}\n"
        )

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

    def on_tool_start(self, name: str, args: dict | None):
        self._emit({"type": "tool_start", "name": name, "args": args})

    def on_tool_end(self, name: str, result: str, is_error: bool):
        self._emit({"type": "tool_end", "name": name, "result": result, "is_error": is_error})

    def on_turn(self, iteration: int):
        self._emit({"type": "turn", "iteration": iteration})

    def on_error(self, message: str):
        self._emit({"type": "error", "message": message})

    def on_complete(self, data: dict):
        self._emit({"type": "complete", **data})

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

    def on_tool_start(self, name: str, args: dict | None):
        pass

    def on_tool_end(self, name: str, result: str, is_error: bool):
        pass

    def on_turn(self, iteration: int):
        pass

    def on_error(self, message: str):
        self._accumulated = ""
        self._complete_data = {"error": message}

    def on_complete(self, data: dict):
        self._complete_data = data

    def flush(self):
        pass

    def print_result(self, session_id: str):
        result = {
            "text": self._accumulated,
            "session_id": session_id,
        }
        if self._complete_data:
            usage = self._complete_data.get("usage")
            if usage:
                result["usage"] = usage
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
                case EventType.TURN_START:
                    printer.on_turn(event.data.get("iteration", 0))
                case EventType.ERROR:
                    printer.on_error(event.data.get("message", "Unknown error"))
                    return None
                case EventType.COMPLETE:
                    printer.on_complete(event.data)
                    return event.data
    except KeyboardInterrupt:
        printer.flush()
        sys.stderr.write(f"\n{_DIM}interrupted{_RESET}\n")
        return None

    printer.flush()
    return None


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

def _resolve_model(args) -> str:
    model = args.model
    if model is None:
        model = os.environ.get("ALOOP_MODEL")
    if model is None:
        sys.stderr.write(
            "error: no model specified. Use --model or set ALOOP_MODEL.\n"
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

    # 3. Credentials file
    if not api_key:
        cred_file = Path.home() / ".aloop" / "credentials.json"
        if cred_file.exists():
            creds = json.loads(cred_file.read_text())
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
                creds = json.loads(cred_file.read_text())
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
            config = json.loads(_ACPX_CONFIG.read_text())
        except (OSError, json.JSONDecodeError):
            pass

    agents = config.get("agents", {})

    expected = {"command": "aloop --acp"}

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
    if not provider_name:
        sys.stderr.write("error: --provider is required for validate-provider\n")
        sys.stderr.write(f"  Example: aloop validate-provider --provider openai --model gpt-4o-mini\n")
        return 1

    model = args.model
    if not model:
        model = os.environ.get("ALOOP_MODEL")
    if not model:
        sys.stderr.write("error: --model is required for validate-provider\n")
        return 1

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

    backend = AgentLoopBackend(model=model, api_key=api_key, provider=provider)
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
                elif event.type == EventType.COMPLETE:
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
# Main
# ---------------------------------------------------------------------------

async def main():
    args = parse_args()

    if args.version:
        from . import __version__
        print(__version__)
        return

    if args.prompt == "update":
        sys.exit(_run_update())

    if args.prompt == "register-acpx":
        sys.exit(_run_register())

    if args.prompt == "list-providers":
        from .providers import get_providers
        providers = get_providers()
        for key, p in sorted(providers.items()):
            status = f"{_GREEN}tested{_RESET}" if p.status == "tested" else f"{_DIM}community{_RESET}"
            print(f"  {key:20s}  {p.name:30s}  [{status}]")
            if p.notes:
                print(f"  {' ':20s}  {_DIM}{p.notes}{_RESET}")
        return

    if args.prompt == "validate-provider":
        sys.exit(await _run_validate_provider(args))

    if args.acp:
        from .acp import serve_acp
        await serve_acp(model=args.model)
        return

    if args.list_models:
        for key, cfg in sorted(get_models().items()):
            print(f"  {key:25s}  {cfg.name}  (ctx: {cfg.context_window:,})")
        return

    if args.prompt == "system-prompt":
        from .system_prompt import _load_aloop_config, _load_template
        from . import get_project_root
        root = get_project_root()
        config = _load_aloop_config(root)
        template = _load_template(root, config)
        if template:
            if "--rendered" in sys.argv:
                print(build_system_prompt())
            else:
                print(template)
        else:
            print(build_system_prompt())
        print(f"\n{_DIM}---{_RESET}")
        print(f"{_DIM}Available variables: {{{{tools}}}}, {{{{skills}}}}, {{{{agents_md}}}}{_RESET}")
        return

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
        backend = AgentLoopBackend(
            model=model,
            api_key=api_key,
            provider=provider,
            max_iterations=args.max_iterations,
        )
    except Exception as e:
        sys.stderr.write(f"error: {e}\n")
        sys.exit(1)

    # --- Build stream kwargs ---
    stream_kw: dict = {"session_key": session_id}

    from .tools import ANALYSIS_TOOLS
    from .tools.skills import load_skill_tool
    tools = list(ANALYSIS_TOOLS)
    if not any(t.name == "load_skill" for t in tools):
        tools = tools + [load_skill_tool]

    if args.tools:
        tool_names = {t.strip() for t in args.tools.split(",")}
        filtered = [t for t in tools if t.name in tool_names]
        unknown = tool_names - {t.name for t in filtered}
        if unknown:
            sys.stderr.write(f"warning: unknown tools: {', '.join(sorted(unknown))}\n")
        tools = filtered

    stream_kw["tools"] = tools
    stream_kw["system_prompt"] = build_system_prompt(tools=tools)

    if args.no_context:
        stream_kw["inject_context"] = False

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
