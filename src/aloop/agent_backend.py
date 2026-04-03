"""Agent loop backend — provider-agnostic, multi-provider."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
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
from .models import ModelConfig, get_model
from .providers import ProviderConfig, get_provider, get_default_provider_name
from .session import AgentSession
from .hooks import run_before_tool, run_register_tools
from .tools import ANALYSIS_TOOLS
from .tools_base import ToolDef, ToolResult
from .types import EventType, InferenceError, InferenceEvent, InferenceResult

MAX_RETRIES = 2
RETRY_DELAY = 1.0
RETRYABLE_CODES = {429, 502, 503, 504}
MAX_TOOL_OUTPUT = 50_000  # absolute hard cap (safety net)
MAX_CONSECUTIVE_COMPACTION_FAILURES = 3




class AgentLoopBackend:
    def __init__(
        self,
        model: str | ModelConfig,
        api_key: str | None = None,
        provider: str | ProviderConfig | None = None,
        max_iterations: int = 50,
        max_retry_delay: float = 60.0,
        compaction_settings: CompactionSettings | None = None,
        max_session_age: float = 14400.0,
        max_session_messages: int = 100,
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
        self.max_iterations = max_iterations
        self.max_retry_delay = max_retry_delay
        self.max_session_age = max_session_age
        self.max_session_messages = max_session_messages

        self._input_tokens = 0
        self._output_tokens = 0
        self.compaction_settings = compaction_settings or CompactionSettings()
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
        system_prompt: str | None = None,
        tools: list[ToolDef] | None = None,
        **kwargs,
    ) -> AsyncIterator[InferenceEvent]:
        if not self.api_key:
            yield InferenceEvent.error("OPENROUTER_API_KEY is not configured")
            return

        if tools is None:
            tools = list(ANALYSIS_TOOLS)
        tool_context = self._build_tool_context(kwargs=kwargs)
        response_format = kwargs.get("response_format")

        session = self._resolve_session(kwargs=kwargs)
        if session:
            messages: list[dict[str, Any]] = list(session.messages)
            self._last_compaction = session.last_compaction
        else:
            messages = []
            self._last_compaction = None

        if prompt:
            user_msg = {"role": "user", "content": prompt}
            messages.append(user_msg)
            if session:
                session.log_message(user_msg)

        system_prompt, injection_messages = await self._build_system_prompt(
            system_prompt=system_prompt,
            inject_context=bool(kwargs.get("inject_context", True)),
            context_kwargs={k: v for k, v in kwargs.items() if k.startswith("topic_")},
        )

        # Inject knowledge messages for fresh sessions
        inject_context = bool(kwargs.get("inject_context", True))
        if inject_context and (not session or not session.messages):
            from .system_prompt import build_knowledge_injections
            knowledge = build_knowledge_injections(
                **{k: v for k, v in kwargs.items() if k.startswith("topic_")},
            )
            if knowledge:
                messages = knowledge + messages
            if injection_messages:
                messages = injection_messages + messages

        tool_schemas = [t.to_schema() for t in tools] if tools else None
        accumulated_text = ""

        self._input_tokens = 0
        self._output_tokens = 0
        self._last_usage = None
        self._last_usage_index = None

        for iteration in range(self.max_iterations):
            yield InferenceEvent(EventType.TURN_START, {"iteration": iteration})

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
                        yield InferenceEvent.text(text_delta)

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
                        yield InferenceEvent.error(delta["message"])
                        if session:
                            session.messages = [m for m in messages if not m.get("_synthetic")]
                            session.last_compaction = self._last_compaction
                            session.save_context()
                        return

            except Exception as exc:
                yield InferenceEvent.error(str(exc))
                if session:
                    session.messages = [m for m in messages if not m.get("_synthetic")]
                    session.last_compaction = self._last_compaction
                    session.save_context()
                return

            yield InferenceEvent(EventType.TURN_END, {"iteration": iteration})

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
                yield InferenceEvent.complete(
                    text=accumulated_text,
                    session_id=session.session_id if session else None,
                    cost_usd=self.cost_usd,
                    usage=self.usage,
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

                yield InferenceEvent.complete(
                    text=accumulated_text,
                    session_id=session.session_id if session else None,
                    cost_usd=self.cost_usd,
                    usage=self.usage,
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

                yield InferenceEvent.tool_start(name, tool_call_id, args)

                tool_def = next((tool for tool in (tools or []) if tool.name == name), None)
                if not tool_def:
                    result = ToolResult(content=f"Unknown tool: {name}", is_error=True)
                else:
                    try:
                        result = await self._execute_tool(tool_def, args, tool_context=tool_context)
                    except Exception as exc:
                        result = ToolResult(content=f"Error: {exc}", is_error=True)

                yield InferenceEvent.tool_end(name, tool_call_id, result.content, result.is_error)

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
                            messages, entry = await compact_context(
                                messages=messages,
                                model_config=self.model_config,
                                settings=self.compaction_settings,
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

        yield InferenceEvent.error(f"Max iterations ({self.max_iterations}) reached")

    async def run(self, prompt: str, **kwargs) -> InferenceResult:
        result: InferenceResult | None = None

        async for event in self.stream(prompt, **kwargs):
            if event.type == EventType.COMPLETE:
                result = InferenceResult(
                    text=event.data.get("text", ""),
                    session_id=event.data.get("session_id"),
                    cost_usd=event.data.get("cost_usd"),
                    usage=event.data.get("usage"),
                )
            elif event.type == EventType.ERROR:
                raise InferenceError(event.data.get("message", "Unknown error"))

        return result or InferenceResult(text="")

    def _resolve_session(self, kwargs: dict) -> AgentSession | None:
        if kwargs.get("persist_session", True) is False:
            return None

        session_id = kwargs.get("session_key") or kwargs.get("session_id")
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

    def _build_tool_context(self, kwargs: dict) -> dict:
        explicit = kwargs.get("tool_context")
        if isinstance(explicit, dict):
            return dict(explicit)

        context: dict[str, Any] = {}
        for key in (
            "topic_id",
            "chat_id",
            "message_thread_id",
            "trigger",
            "session_key",
        ):
            value = kwargs.get(key)
            if value is not None:
                context[key] = value
        return context

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
