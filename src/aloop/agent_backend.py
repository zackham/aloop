"""Agent loop backend — provider-agnostic, multi-provider."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from .compaction import (
    CompactionEntry,
    CompactionSettings,
    compact_context,
    estimate_context_tokens,
    get_compaction_settings,
    persist_tool_result,
    restore_recent_files,
    should_compact,
)
from .config import LoopConfig, load_mode, resolve_mode_system_prompt
from .models import ModelConfig, get_model
from .providers import ProviderConfig, get_provider, get_default_provider_name
from .types import ModeConflictError
from .session import AgentSession
from .hooks import (
    run_before_tool,
    run_register_tools,
    run_on_loop_start,
    run_on_loop_end,
    run_on_turn_start,
    run_on_turn_end,
    run_on_pre_compaction,
    run_on_post_compaction,
)
from .tools import ANALYSIS_TOOLS
from .tools_base import ToolDef, ToolRejected, ToolResult
from .types import EventType, InferenceError, InferenceEvent, RunResult

MAX_RETRIES = 2
RETRY_DELAY = 1.0
RETRYABLE_CODES = {429, 502, 503, 504}
MAX_TOOL_OUTPUT = 50_000  # absolute hard cap (safety net)
MAX_CONSECUTIVE_COMPACTION_FAILURES = 3




class ALoop:
    def __init__(
        self,
        model: str | ModelConfig,
        api_key: str | None = None,
        provider: str | ProviderConfig | None = None,
        config: LoopConfig | None = None,
        max_retry_delay: float = 60.0,
        # Deprecated individual params — use config instead
        max_iterations: int | None = None,
        compaction_settings: CompactionSettings | None = None,
        max_session_age: float | None = None,
        max_session_messages: int | None = None,
    ):
        self.model_config = get_model(model) if isinstance(model, str) else model

        # Resolve provider
        if isinstance(provider, ProviderConfig):
            self.provider = provider
        elif isinstance(provider, str):
            self.provider = get_provider(provider)
        else:
            self.provider = get_provider(get_default_provider_name())

        # Resolve API key: explicit > provider-specific env var > generic
        self.api_key = (
            api_key
            or (os.environ.get(self.provider.env_key, "") if self.provider.env_key else "")
            or os.environ.get("ALOOP_API_KEY", "")
        )

        # Build effective config: explicit config > individual params > defaults
        if config is None:
            config = LoopConfig()
        self.config = config

        # Individual params override config for backward compat
        if max_iterations is not None:
            self.config.max_iterations = max_iterations
        if max_session_age is not None:
            self.config.max_session_age = max_session_age
        if max_session_messages is not None:
            self.config.max_session_messages = max_session_messages
        if compaction_settings is not None:
            self.config.compaction = compaction_settings

        # Convenience accessors
        self.max_iterations = self.config.max_iterations
        self.max_retry_delay = max_retry_delay
        self.max_session_age = self.config.max_session_age
        self.max_session_messages = self.config.max_session_messages

        self._session_modes: dict[str, str] = {}  # session_id -> mode_name

        # Store constructor defaults (mode overrides are per-stream-call)
        self._default_model_config = self.model_config
        self._default_provider = self.provider
        self._default_compaction = self.config.compaction
        self._default_max_iterations = self.config.max_iterations

        self._input_tokens = 0
        self._output_tokens = 0
        self.compaction_settings = self.config.compaction
        self._last_compaction: CompactionEntry | None = None
        self._last_usage: dict | None = None
        self._last_usage_index: int | None = None
        self._compaction_failures: int = 0

    @property
    def cost_usd(self) -> float:
        return (
            self._input_tokens * self.model_config.cost_input / 1_000_000
            + self._output_tokens * self.model_config.cost_output / 1_000_000
        )

    @property
    def usage(self) -> dict:
        return {
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "cost_usd": self.cost_usd,
            "model": self.model_config.id,
        }

    async def stream(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        tools: list[ToolDef] | None = None,
        extra_tools: list[ToolDef] | None = None,
        session_id: str | None = None,
        mode: str | None = None,
        persist_session: bool = True,
        inject_context: bool = True,
        response_format: dict | None = None,
        context: dict | None = None,
        # Deprecated — use session_id
        session_key: str | None = None,
        **kwargs,
    ) -> AsyncIterator[InferenceEvent]:
        if not self.api_key:
            yield InferenceEvent.error("OPENROUTER_API_KEY is not configured")
            return

        # Backward compat: session_key → session_id
        if session_id is None and session_key is not None:
            session_id = session_key

        # ── Mode resolution ──────────────────────────────────────────
        # Precedence: explicit stream() kwargs > mode config > constructor defaults
        mode_config: dict = {}
        effective_model_config = self._default_model_config
        effective_provider = self._default_provider
        effective_compaction = self._default_compaction
        effective_max_iterations = self._default_max_iterations

        if mode is not None:
            # Load project config for mode definitions
            from .system_prompt import _load_aloop_config
            from . import get_project_root
            project_root = get_project_root()
            project_config = _load_aloop_config(project_root)
            mode_config = load_mode(mode, project_config)

            # ModeConflictError: same session, different mode
            if session_id and session_id in self._session_modes:
                existing_mode = self._session_modes[session_id]
                if existing_mode != mode:
                    raise ModeConflictError(
                        f"Session {session_id!r} was created with mode {existing_mode!r}, "
                        f"cannot switch to {mode!r}. Create a new session."
                    )

            # Track mode for this session
            if session_id:
                self._session_modes[session_id] = mode

            # Apply mode's model (unless overridden by explicit stream kwargs —
            # we can't detect that here, so model/provider in mode only apply
            # if they are present in mode_config)
            if "model" in mode_config:
                effective_model_config = get_model(mode_config["model"])
            if "provider" in mode_config:
                p = mode_config["provider"]
                effective_provider = get_provider(p) if isinstance(p, str) else p
            if "compaction" in mode_config:
                mc = mode_config["compaction"]
                effective_compaction = CompactionSettings(
                    enabled=mc.get("enabled", effective_compaction.enabled),
                    reserve_tokens=mc.get("reserve_tokens", effective_compaction.reserve_tokens),
                    keep_recent_tokens=mc.get("keep_recent_tokens", effective_compaction.keep_recent_tokens),
                    max_tool_result_chars=mc.get("max_tool_result_chars", effective_compaction.max_tool_result_chars),
                    compact_instructions=mc.get("compact_instructions", effective_compaction.compact_instructions),
                )
            if "max_iterations" in mode_config:
                effective_max_iterations = mode_config["max_iterations"]

            # Apply mode's system_prompt (unless caller passed explicit system_prompt)
            if system_prompt is None:
                mode_sp = resolve_mode_system_prompt(mode_config)
                if mode_sp is not None:
                    system_prompt = mode_sp
        else:
            # No mode — still track session as having no mode for conflict detection
            if session_id and session_id in self._session_modes:
                existing_mode = self._session_modes[session_id]
                if existing_mode is not None:
                    raise ModeConflictError(
                        f"Session {session_id!r} was created with mode {existing_mode!r}, "
                        f"cannot switch to no mode. Create a new session."
                    )

        # Tool merge behavior:
        # 1. If tools= is explicitly set → REPLACE entire set (explicit override)
        # 2. Otherwise: start with mode's tool list (or defaults),
        #    add register_tools hooks, add extra_tools
        if tools is not None:
            # Explicit override — use exactly what was passed
            pass
        else:
            if mode_config.get("tools"):
                # Mode defines a tool whitelist — filter defaults by name
                all_available = list(ANALYSIS_TOOLS)
                hook_tools = run_register_tools()
                if hook_tools:
                    all_available.extend(hook_tools)
                allowed_names = set(mode_config["tools"])
                tools = [t for t in all_available if t.name in allowed_names]
            else:
                # Start with defaults
                tools = list(ANALYSIS_TOOLS)
                # Add tools from register_tools hooks
                hook_tools = run_register_tools()
                if hook_tools:
                    tools.extend(hook_tools)
            # Add extra_tools if provided
            if extra_tools:
                tools.extend(extra_tools)

        # Apply effective config for this stream call (mode may override).
        # These are set per-call; _default_* stores the constructor values.
        self.model_config = effective_model_config
        self.provider = effective_provider
        self.compaction_settings = effective_compaction
        self.max_iterations = effective_max_iterations

        # Build tool context from explicit context dict + any legacy kwargs
        tool_context = self._build_tool_context(context=context, kwargs=kwargs)

        session = self._resolve_session(
            session_id=session_id, persist_session=persist_session,
        )
        if session:
            messages: list[dict[str, Any]] = list(session.messages)
            self._last_compaction = session.last_compaction
        else:
            messages = []
            self._last_compaction = None

        # Resolve the effective session_id for event tagging
        effective_session_id = session.session_id if session else session_id

        if prompt:
            user_msg = {"role": "user", "content": prompt}
            messages.append(user_msg)
            if session:
                session.log_message(user_msg)

        system_prompt, injection_messages = await self._build_system_prompt(
            system_prompt=system_prompt,
            inject_context=inject_context,
            context_kwargs={k: v for k, v in kwargs.items() if k.startswith("topic_")},
        )

        tool_schemas = [t.to_schema() for t in tools] if tools else None
        accumulated_text = ""

        self._input_tokens = 0
        self._output_tokens = 0
        self._last_usage = None
        self._last_usage_index = None

        # Emit LOOP_START before the first turn
        yield InferenceEvent(
            EventType.LOOP_START,
            {
                "session_id": effective_session_id,
                "model": self.model_config.id,
                "provider": self.provider.name,
            },
            session_id=effective_session_id,
        )

        # Hook: on_loop_start
        loop_hook_context = {
            "session_id": effective_session_id,
            "model": self.model_config.id,
            "provider": self.provider.name,
            **(tool_context or {}),
        }
        run_on_loop_start(loop_hook_context)

        iteration_count = 0
        for iteration in range(self.max_iterations):
            iteration_count = iteration + 1
            turn_id = uuid.uuid4().hex[:12]

            # Track per-turn token deltas
            turn_input_before = self._input_tokens
            turn_output_before = self._output_tokens

            yield InferenceEvent(
                EventType.TURN_START,
                {"iteration": iteration, "turn_id": turn_id},
                session_id=effective_session_id,
                turn_id=turn_id,
            )

            # Hook: on_turn_start
            run_on_turn_start({
                "session_id": effective_session_id,
                "iteration": iteration,
                "turn_id": turn_id,
                **(tool_context or {}),
            })

            content = ""
            tool_calls: list[dict[str, Any]] = []
            turn_usage: dict | None = None

            try:
                async for delta in self._stream_completion(messages, system_prompt, tool_schemas, response_format=response_format):
                    delta_type = delta.get("type")

                    if delta_type == "text":
                        text_delta = delta["text"]
                        content += text_delta
                        accumulated_text += text_delta
                        evt = InferenceEvent.text(text_delta)
                        evt.session_id = effective_session_id
                        evt.turn_id = turn_id
                        yield evt

                    elif delta_type == "tool_call_delta":
                        self._accumulate_tool_call(tool_calls, delta)

                    elif delta_type == "usage":
                        usage = delta.get("usage") or {}
                        turn_usage = usage
                        self._input_tokens += int(
                            usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
                        )
                        self._output_tokens += int(
                            usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
                        )

                    elif delta_type == "error":
                        evt = InferenceEvent.error(delta["message"])
                        evt.session_id = effective_session_id
                        evt.turn_id = turn_id
                        yield evt
                        if session:
                            session.messages = [m for m in messages if not m.get("_synthetic")]
                            session.last_compaction = self._last_compaction
                            session.save_context()
                        return

            except Exception as exc:
                evt = InferenceEvent.error(str(exc))
                evt.session_id = effective_session_id
                evt.turn_id = turn_id
                yield evt
                if session:
                    session.messages = [m for m in messages if not m.get("_synthetic")]
                    session.last_compaction = self._last_compaction
                    session.save_context()
                return

            # Compute per-turn token deltas and cost
            turn_input_tokens = self._input_tokens - turn_input_before
            turn_output_tokens = self._output_tokens - turn_output_before
            turn_cost = (
                turn_input_tokens * self.model_config.cost_input / 1_000_000
                + turn_output_tokens * self.model_config.cost_output / 1_000_000
            ) if (turn_input_tokens or turn_output_tokens) else None

            assistant_msg: dict[str, Any]
            if tool_calls:
                assistant_msg = {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [
                        {
                            "id": tool_call.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": tool_call.get("function", {}).get("name", ""),
                                "arguments": tool_call.get("function", {}).get("arguments", ""),
                            },
                        }
                        for tool_call in tool_calls
                    ],
                }
            else:
                assistant_msg = {"role": "assistant", "content": content}

            # Empty response guard — model returned no content and no tool_calls
            if not tool_calls and not content:
                if session:
                    session.log_event("empty_response", {"iteration": iteration})
                    session.messages = [m for m in messages if not m.get("_synthetic")]
                    session.last_compaction = self._last_compaction
                    session.save_context()
                turn_end_data = {
                    "iteration": iteration,
                    "turn_id": turn_id,
                    "input_tokens": turn_input_tokens,
                    "output_tokens": turn_output_tokens,
                    "cost_usd": turn_cost,
                }
                yield InferenceEvent(
                    EventType.TURN_END,
                    turn_end_data,
                    session_id=effective_session_id,
                    turn_id=turn_id,
                )
                run_on_turn_end(
                    {"session_id": effective_session_id, "iteration": iteration, "turn_id": turn_id, **(tool_context or {})},
                    turn_end_data,
                )
                loop_end_data = {
                    "text": accumulated_text,
                    "session_id": effective_session_id,
                    "input_tokens": self._input_tokens,
                    "output_tokens": self._output_tokens,
                    "cost_usd": self.cost_usd or None,
                    "model": self.model_config.id,
                    "turns": iteration_count,
                }
                run_on_loop_end(loop_hook_context, loop_end_data)
                yield InferenceEvent(
                    EventType.LOOP_END,
                    loop_end_data,
                    session_id=effective_session_id,
                )
                return

            messages.append(assistant_msg)
            if turn_usage:
                self._last_usage = turn_usage
                self._last_usage_index = len(messages) - 1

            if session:
                session.log_message(assistant_msg)

            if not tool_calls:
                if session:
                    session.messages = [m for m in messages if not m.get("_synthetic")]
                    session.last_compaction = self._last_compaction
                    session.save_context()

                turn_end_data = {
                    "iteration": iteration,
                    "turn_id": turn_id,
                    "input_tokens": turn_input_tokens,
                    "output_tokens": turn_output_tokens,
                    "cost_usd": turn_cost,
                }
                yield InferenceEvent(
                    EventType.TURN_END,
                    turn_end_data,
                    session_id=effective_session_id,
                    turn_id=turn_id,
                )
                run_on_turn_end(
                    {"session_id": effective_session_id, "iteration": iteration, "turn_id": turn_id, **(tool_context or {})},
                    turn_end_data,
                )
                loop_end_data = {
                    "text": accumulated_text,
                    "session_id": effective_session_id,
                    "input_tokens": self._input_tokens,
                    "output_tokens": self._output_tokens,
                    "cost_usd": self.cost_usd or None,
                    "model": self.model_config.id,
                    "turns": iteration_count,
                }
                run_on_loop_end(loop_hook_context, loop_end_data)
                yield InferenceEvent(
                    EventType.LOOP_END,
                    loop_end_data,
                    session_id=effective_session_id,
                )
                return

            for tool_call in tool_calls:
                function_data = tool_call.get("function", {})
                name = function_data.get("name", "")
                tool_call_id = tool_call.get("id", "")

                try:
                    args = json.loads(function_data.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}

                evt = InferenceEvent.tool_start(name, tool_call_id, args)
                evt.session_id = effective_session_id
                evt.turn_id = turn_id
                yield evt

                tool_def = next((tool for tool in (tools or []) if tool.name == name), None)
                if not tool_def:
                    result = ToolResult(content=f"Unknown tool: {name}", is_error=True)
                else:
                    try:
                        result = await self._execute_tool(tool_def, args, tool_context=tool_context)
                    except Exception as exc:
                        result = ToolResult(content=f"Error: {exc}", is_error=True)

                evt = InferenceEvent.tool_end(name, tool_call_id, result.content, result.is_error)
                evt.session_id = effective_session_id
                evt.turn_id = turn_id
                yield evt

                overflow_dir = session.session_dir / f"{session.session_id}_tool_results" if session else None
                persisted = persist_tool_result(
                    result.content[:MAX_TOOL_OUTPUT],
                    tool_name=name,
                    tool_call_id=tool_call_id,
                    overflow_dir=overflow_dir,
                    max_chars=self.compaction_settings.max_tool_result_chars,
                )
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": persisted,
                }
                messages.append(tool_msg)
                if session:
                    session.log_message(tool_msg)

            # Emit TURN_END after tool execution, before compaction/next iteration
            turn_end_data = {
                "iteration": iteration,
                "turn_id": turn_id,
                "input_tokens": turn_input_tokens,
                "output_tokens": turn_output_tokens,
                "cost_usd": turn_cost,
            }
            yield InferenceEvent(
                EventType.TURN_END,
                turn_end_data,
                session_id=effective_session_id,
                turn_id=turn_id,
            )
            run_on_turn_end(
                {"session_id": effective_session_id, "iteration": iteration, "turn_id": turn_id, **(tool_context or {})},
                turn_end_data,
            )

            if self.compaction_settings.enabled:
                # Full compaction — only if over threshold
                if self._compaction_failures < MAX_CONSECUTIVE_COMPACTION_FAILURES:
                    try:
                        ctx_tokens = estimate_context_tokens(
                            messages,
                            self._last_usage,
                            self._last_usage_index,
                        )
                        if should_compact(
                            ctx_tokens,
                            self.model_config.context_window,
                            self.compaction_settings,
                        ):
                            # Hook: on_pre_compaction
                            compaction_hook_ctx = {
                                "session_id": effective_session_id,
                                "context_tokens": ctx_tokens,
                                "message_count": len(messages),
                                **(tool_context or {}),
                            }
                            extra_instructions = run_on_pre_compaction(compaction_hook_ctx)

                            messages_before_count = len(messages)
                            tokens_before = ctx_tokens

                            # If hooks returned extra instructions, temporarily
                            # augment compaction settings
                            effective_settings = self.compaction_settings
                            if extra_instructions:
                                existing = self.compaction_settings.compact_instructions or ""
                                combined = f"{existing}\n\n{extra_instructions}".strip() if existing else extra_instructions
                                effective_settings = CompactionSettings(
                                    enabled=self.compaction_settings.enabled,
                                    reserve_tokens=self.compaction_settings.reserve_tokens,
                                    keep_recent_tokens=self.compaction_settings.keep_recent_tokens,
                                    max_tool_result_chars=self.compaction_settings.max_tool_result_chars,
                                    compact_instructions=combined,
                                )

                            messages, entry = await compact_context(
                                messages=messages,
                                model_config=self.model_config,
                                settings=effective_settings,
                                call_model_fn=self._summarize,
                                previous_entry=self._last_compaction,
                            )
                            if entry:
                                self._compaction_failures = 0
                                self._last_compaction = entry

                                # Post-compaction file restoration
                                restoration = restore_recent_files(
                                    entry.file_ops,
                                    kept_messages=messages[1:],
                                )
                                if restoration:
                                    messages = messages[:1] + restoration + messages[1:]

                                messages_after_count = len(messages)
                                tokens_after = estimate_context_tokens(
                                    messages,
                                    self._last_usage,
                                    self._last_usage_index,
                                )

                                yield InferenceEvent(
                                    EventType.COMPACTION,
                                    {
                                        "messages_before": messages_before_count,
                                        "messages_after": messages_after_count,
                                        "tokens_saved": tokens_before - tokens_after,
                                    },
                                    session_id=effective_session_id,
                                )

                                # Hook: on_post_compaction
                                run_on_post_compaction({
                                    "session_id": effective_session_id,
                                    "messages_before": messages_before_count,
                                    "messages_after": messages_after_count,
                                    "tokens_saved": tokens_before - tokens_after,
                                    **(tool_context or {}),
                                })

                                if session:
                                    session.log_event(
                                        "compaction",
                                        {
                                            "timestamp": entry.timestamp,
                                            "tokens_before": entry.tokens_before,
                                            "first_kept_index": entry.first_kept_index,
                                            "files_restored": len(restoration),
                                        },
                                    )
                    except Exception as exc:
                        self._compaction_failures += 1
                        if session:
                            session.log_event(
                                "compaction_error",
                                {
                                    "error": str(exc),
                                    "consecutive_failures": self._compaction_failures,
                                },
                            )
                        if self._compaction_failures >= MAX_CONSECUTIVE_COMPACTION_FAILURES:
                            if session:
                                session.log_event(
                                    "compaction_circuit_breaker",
                                    {"failures": self._compaction_failures},
                                )

            if session:
                session.messages = [m for m in messages if not m.get("_synthetic")]
                session.last_compaction = self._last_compaction
                session.save_context()

        if session:
            session.messages = [m for m in messages if not m.get("_synthetic")]
            session.last_compaction = self._last_compaction
            session.save_context()

        # Hook: on_loop_end (max iterations reached)
        run_on_loop_end(loop_hook_context, {
            "text": accumulated_text,
            "session_id": effective_session_id,
            "error": f"Max iterations ({self.max_iterations}) reached",
            "turns": iteration_count,
        })
        yield InferenceEvent.error(f"Max iterations ({self.max_iterations}) reached")

    async def run(self, prompt: str, **kwargs) -> RunResult:
        result: RunResult | None = None

        async for event in self.stream(prompt, **kwargs):
            if event.type == EventType.LOOP_END:
                result = RunResult(
                    text=event.data.get("text", ""),
                    session_id=event.data.get("session_id"),
                    input_tokens=event.data.get("input_tokens", 0),
                    output_tokens=event.data.get("output_tokens", 0),
                    cost_usd=event.data.get("cost_usd"),
                    model=event.data.get("model"),
                    turns=event.data.get("turns", 0),
                )
            elif event.type == EventType.ERROR:
                raise InferenceError(event.data.get("message", "Unknown error"))

        return result or RunResult(text="")

    def _resolve_session(
        self,
        session_id: str | None = None,
        persist_session: bool = True,
        # Deprecated: accept kwargs dict for backward compat
        kwargs: dict | None = None,
    ) -> AgentSession | None:
        # Backward compat: pull from kwargs if explicit params not set
        if kwargs is not None:
            if persist_session and kwargs.get("persist_session", True) is False:
                persist_session = False
            if session_id is None:
                session_id = kwargs.get("session_key") or kwargs.get("session_id")

        if not persist_session:
            return None

        if not session_id:
            return None

        session = AgentSession.get_or_create(session_id=session_id)
        if session.messages and session.is_stale(
            max_age_seconds=self.max_session_age,
            max_messages=self.max_session_messages,
        ):
            session.log_event("session_auto_cleared", {
                "reason": "stale",
                "age_seconds": time.time() - session.last_active,
                "message_count": len(session.messages),
            })
            session.clear()
        return session

    async def _build_system_prompt(
        self,
        system_prompt: str | None,
        inject_context: bool,
        context_kwargs: dict | None = None,
    ) -> tuple[str | None, list[dict]]:
        # If no system prompt provided, build default harness sections
        if system_prompt is None:
            from .system_prompt import build_system_prompt
            system_prompt = build_system_prompt()

        if not inject_context:
            return system_prompt, []

        # If caller already provided a full system prompt (e.g. cli.py with
        # build_system_prompt()), skip hook-based injection to avoid duplication.
        if system_prompt:
            return system_prompt, []

        # Hook-based context injection
        from .hooks import run_gather_context
        context = run_gather_context("", **(context_kwargs or {}))
        if context:
            context_block = f"## Current Context\n\n{context}"
            return context_block, []

        return system_prompt, []

    async def _summarize(
        self,
        messages: list[dict],
        system_prompt: str,
        model: ModelConfig,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model.id,
            "messages": [{"role": "system", "content": system_prompt}] + messages,
            "max_tokens": min(4096, model.max_output),
            "stream": False,
        }

        if self.provider.supports_provider_routing and model.provider_order:
            payload["provider"] = {"order": list(model.provider_order)}

        headers = {"Authorization": f"Bearer {self.api_key}"}
        headers.update(self.provider.extra_headers)

        timeout = httpx.Timeout(timeout=120.0, connect=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(self.provider.base_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
            return "".join(text_parts)
        return str(content)

    def _build_tool_context(
        self,
        context: dict | None = None,
        kwargs: dict | None = None,
    ) -> dict:
        # Explicit context dict takes priority
        if context is not None:
            return dict(context)

        # Legacy: build from kwargs
        if kwargs is None:
            return {}

        explicit = kwargs.get("tool_context")
        if isinstance(explicit, dict):
            return dict(explicit)

        result: dict[str, Any] = {}
        for key in (
            "topic_id",
            "chat_id",
            "message_thread_id",
            "trigger",
            "session_key",
            "session_id",
        ):
            value = kwargs.get(key)
            if value is not None:
                result[key] = value
        return result

    async def _execute_tool(
        self,
        tool_def: ToolDef,
        args: dict,
        *,
        tool_context: dict | None = None,
    ) -> ToolResult:
        # Run before_tool hooks (permissions, firebreaks, etc.)
        hook_result = run_before_tool(
            tool_def.name, args, **(tool_context or {}),
        )
        if not hook_result.get("allow", True):
            # Check for ToolResult short-circuit from hook
            if "tool_result" in hook_result:
                return hook_result["tool_result"]
            return ToolResult(
                content=hook_result.get("reason", f"Blocked by hook: {tool_def.name}"),
                is_error=True,
            )
        if "args" in hook_result:
            args = hook_result["args"]

        call_args = dict(args)
        if tool_context:
            try:
                sig = inspect.signature(tool_def.execute)
                accepts_var_kwargs = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    for p in sig.parameters.values()
                )
                if "_context" in sig.parameters or accepts_var_kwargs:
                    call_args["_context"] = dict(tool_context)
            except (TypeError, ValueError):  # pragma: no cover - builtins/callables without signature
                pass

        result = tool_def.execute(**call_args)
        if inspect.isawaitable(result):
            result = await result

        if isinstance(result, ToolResult):
            return result
        if isinstance(result, dict):
            return ToolResult(content=json.dumps(result))
        return ToolResult(content=str(result))

    def _accumulate_tool_call(self, tool_calls: list[dict], delta: dict) -> None:
        idx = int(delta.get("index", 0) or 0)
        while idx >= len(tool_calls):
            tool_calls.append({"id": "", "function": {"name": "", "arguments": ""}})

        if delta.get("id"):
            tool_calls[idx]["id"] = delta["id"]

        fn = delta.get("function", {})
        if fn.get("name"):
            tool_calls[idx]["function"]["name"] = fn["name"]
        if fn.get("arguments"):
            tool_calls[idx]["function"]["arguments"] += fn["arguments"]

    async def _stream_completion(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str | None,
        tools: list[dict] | None,
        response_format: dict | None = None,
    ) -> AsyncIterator[dict]:
        payload: dict[str, Any] = {
            "model": self.model_config.id,
            "messages": (
                ([{"role": "system", "content": system_prompt}] if system_prompt else [])
                + [{k: v for k, v in m.items() if k != "_synthetic"} for m in messages]
            ),
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": min(8192, self.model_config.max_output),
        }

        if response_format:
            payload["response_format"] = response_format

        if tools and self.model_config.supports_tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        if self.provider.supports_provider_routing and self.model_config.provider_order:
            payload["provider"] = {"order": list(self.model_config.provider_order)}

        headers = {"Authorization": f"Bearer {self.api_key}"}
        headers.update(self.provider.extra_headers)

        for attempt in range(MAX_RETRIES + 1):
            try:
                timeout = httpx.Timeout(timeout=self.model_config.stream_timeout, connect=30.0)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream(
                        "POST",
                        self.provider.base_url,
                        json=payload,
                        headers=headers,
                    ) as response:
                        if response.status_code in RETRYABLE_CODES and attempt < MAX_RETRIES:
                            delay = min(self.max_retry_delay, RETRY_DELAY * (2**attempt))
                            await asyncio.sleep(delay)
                            continue

                        if response.status_code != 200:
                            body = (await response.aread()).decode(errors="replace")
                            yield {
                                "type": "error",
                                "message": f"API {response.status_code}: {body[:500]}",
                            }
                            return

                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue

                            raw = line[5:].strip()
                            if raw == "[DONE]":
                                break

                            try:
                                chunk = json.loads(raw)
                            except json.JSONDecodeError:
                                continue

                            if chunk.get("error"):
                                message = chunk["error"].get("message", "Unknown API error")
                                yield {"type": "error", "message": message}
                                return

                            choice = (chunk.get("choices") or [{}])[0]
                            delta = choice.get("delta", {})

                            if delta.get("content"):
                                yield {"type": "text", "text": delta["content"]}

                            if delta.get("tool_calls"):
                                for tool_call in delta["tool_calls"]:
                                    merged = {**tool_call, "type": "tool_call_delta"}
                                    yield merged

                            if chunk.get("usage"):
                                yield {"type": "usage", "usage": chunk["usage"]}

                    return

            except httpx.TimeoutException:
                if attempt < MAX_RETRIES:
                    delay = min(self.max_retry_delay, RETRY_DELAY * (2**attempt))
                    await asyncio.sleep(delay)
                    continue

                yield {"type": "error", "message": "Request timed out after retries"}
                return

            except httpx.HTTPError as exc:
                if attempt < MAX_RETRIES:
                    delay = min(self.max_retry_delay, RETRY_DELAY * (2**attempt))
                    await asyncio.sleep(delay)
                    continue

                yield {"type": "error", "message": f"HTTP error: {exc}"}
                return


# Deprecated alias — use ALoop instead
AgentLoopBackend = ALoop
