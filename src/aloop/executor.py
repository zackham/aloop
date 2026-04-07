"""AgentExecutor protocol and InProcessExecutor implementation.

The executor is the abstraction boundary between the agent tool and the
mechanism that runs a child agent. v0.6.0 ships InProcessExecutor as the
sole implementation. The protocol exists so that a future subprocess
backend can be added without touching the agent tool.
"""

from __future__ import annotations

import asyncio
import time
import uuid as _uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from .agent_result import AgentResult, FORK_BOILERPLATE, extract_partial_result
from .session import AgentSession
from .types import EventType, InferenceError

if TYPE_CHECKING:
    from .agent_backend import ALoop


@dataclass
class AgentExecutionHandle:
    """Handle returned by AgentExecutor.spawn().

    Holds an asyncio.Task that resolves to the child's AgentResult.
    Callers can await result() or cancel() the underlying task.
    """

    session_id: str
    spawn_kind: Literal["fork", "fresh"]
    mode: str | None
    parent_session_id: str | None
    parent_turn_id: str | None
    _task: asyncio.Task

    async def result(self) -> AgentResult:
        return await self._task

    def cancel(self) -> bool:
        return self._task.cancel()


@runtime_checkable
class AgentExecutor(Protocol):
    """Protocol for objects that can spawn child agents.

    Implementations must provide an async ``spawn`` method that returns
    an AgentExecutionHandle. The handle wraps an in-flight task; the
    caller awaits handle.result() to receive the AgentResult.
    """

    async def spawn(
        self,
        *,
        prompt: str,
        mode: str | None,
        model: str | None,
        parent_session_id: str | None,
        parent_turn_id: str | None,
        fork: bool,
        parent_loop: "ALoop",
    ) -> AgentExecutionHandle: ...


class InProcessExecutor:
    """Default executor: runs child agents in the same Python process.

    Fork-path children reuse the parent's ALoop instance (so they share
    its provider/api_key/config and the parent's per-stream state).
    Fresh-path children get a new ALoop built from the parent's defaults.
    """

    async def spawn(
        self,
        *,
        prompt: str,
        mode: str | None,
        model: str | None,
        parent_session_id: str | None,
        parent_turn_id: str | None,
        fork: bool,
        parent_loop: "ALoop",
    ) -> AgentExecutionHandle:
        # Snapshot parent state we need to preserve across the child run.
        # The fork path reuses parent_loop (same instance), so the child
        # stream WILL clobber per-stream state — we restore after.
        #
        # NOTE on token counters: _input_tokens and _output_tokens are
        # snapshotted, but the restore logic ADDS the child's usage to
        # the snapshot value rather than overwriting. This way child
        # token usage rolls up into parent.cost_usd / parent.usage,
        # while still preserving the parent's pre-fork tally (which the
        # child's stream() call would otherwise reset to 0).
        parent_state_snapshot: dict | None = None
        if fork:
            if parent_session_id is None or parent_turn_id is None:
                raise ValueError(
                    "Fork-path spawn requires parent_session_id and parent_turn_id "
                    "(parent must be running with persist_session=True)"
                )
            # Save parent's current session before forking so the fork
            # operation reads the latest turn from disk.
            parent_session = getattr(parent_loop, "_current_session", None)
            if parent_session is not None:
                try:
                    parent_session.save_context()
                except OSError:
                    pass
            parent_state_snapshot = {
                "model_config": parent_loop.model_config,
                "provider": parent_loop.provider,
                "compaction_settings": parent_loop.compaction_settings,
                "max_iterations": parent_loop.max_iterations,
                "_current_mode_name": parent_loop._current_mode_name,
                "_current_session": parent_loop._current_session,
                "_active_permissions": parent_loop._active_permissions,
                "_active_allowed_tools": parent_loop._active_allowed_tools,
                "_last_compaction": parent_loop._last_compaction,
                "_last_usage": parent_loop._last_usage,
                "_last_usage_index": parent_loop._last_usage_index,
            }
            # Captured separately because the restore semantics differ:
            # tokens accumulate, other state is overwritten.
            parent_input_tokens_pre = parent_loop._input_tokens
            parent_output_tokens_pre = parent_loop._output_tokens
            child_loop = parent_loop  # reuse parent's ALoop instance
            wrapped_prompt = FORK_BOILERPLATE + prompt
            spawn_kind: Literal["fork", "fresh"] = "fork"
            child_mode: str | None = None
            stream_kwargs: dict = dict(
                prompt=wrapped_prompt,
                fork_from=parent_session_id,
                fork_at=parent_turn_id,
            )
        else:
            child_loop = self._build_fresh_loop(parent_loop, model=model)
            spawn_kind = "fresh"
            child_mode = mode
            parent_input_tokens_pre = 0
            parent_output_tokens_pre = 0
            # Generate a session_id for the child so spawn_metadata can
            # be persisted (fresh children otherwise have no place to
            # store lineage info). Retry on collision (very rare with
            # 64-bit space, but possible over a long-lived process).
            child_session_id = _generate_unique_session_id()
            stream_kwargs = dict(
                prompt=prompt,
                mode=mode,
                session_id=child_session_id,
            )

        spawning_mode = getattr(parent_loop, "_current_mode_name", None)

        async def _run() -> AgentResult:
            text = ""
            session_id = ""
            usage_stats: dict = {}
            error_msg: str | None = None

            try:
                async for event in child_loop.stream(**stream_kwargs):
                    if event.type == EventType.LOOP_START:
                        sid = event.data.get("session_id")
                        if sid:
                            session_id = sid
                    elif event.type == EventType.LOOP_END:
                        text = event.data.get("text", "") or ""
                        sid = event.data.get("session_id")
                        if sid:
                            session_id = sid
                        usage_stats = {
                            "input_tokens": event.data.get("input_tokens", 0),
                            "output_tokens": event.data.get("output_tokens", 0),
                            "cost_usd": event.data.get("cost_usd"),
                            "model": event.data.get("model"),
                            "turns": event.data.get("turns", 0),
                        }
                    elif event.type == EventType.ERROR:
                        error_msg = event.data.get("message", "child agent error")
            finally:
                # Restore parent state on the same instance for the fork
                # path. The child stream() clobbers per-stream attributes;
                # the parent must continue with its original config.
                if parent_state_snapshot is not None:
                    for k, v in parent_state_snapshot.items():
                        setattr(parent_loop, k, v)
                    # Token counters: ADD child usage to parent's
                    # pre-fork tally so child consumption rolls up into
                    # parent.cost_usd / parent.usage. The child stream()
                    # call reset the counters to 0 then accumulated its
                    # own usage, so the current values represent
                    # exactly the child's usage delta.
                    parent_loop._input_tokens = (
                        parent_input_tokens_pre + parent_loop._input_tokens
                    )
                    parent_loop._output_tokens = (
                        parent_output_tokens_pre + parent_loop._output_tokens
                    )

            # Load the child session ONCE and reuse it for both partial
            # result extraction and spawn_metadata persistence. This must
            # happen even on the error path so users can see lineage of
            # failed runs.
            child_sess: AgentSession | None = None
            if session_id:
                child_sess = AgentSession.load(session_id)

            # Result-extraction fallback: if loop ended without a final
            # assistant text (max_iterations reached), pull last assistant
            # text block from the child session's OWN messages.
            #
            # CRITICAL: For fork-path children, resolve_messages() walks
            # the parent chain — meaning the most recent assistant text
            # might come from the PARENT, not the child. We must use
            # child_sess.messages directly so the fallback never returns
            # the parent's text as the child's output.
            if not text and child_sess is not None:
                text = extract_partial_result(child_sess.messages)

            # Persist spawn metadata to child's context.json for lineage.
            # This MUST run before raising InferenceError so errored
            # children still get spawn_metadata written.
            if child_sess is not None:
                _write_spawn_metadata(
                    child_sess,
                    spawn_kind=spawn_kind,
                    parent_session_id=parent_session_id,
                    parent_turn_id=parent_turn_id,
                    spawning_mode=spawning_mode,
                    child_mode=child_mode,
                )

            if error_msg is not None:
                raise InferenceError(error_msg)

            return AgentResult(
                text=text,
                session_id=session_id,
                spawn_kind=spawn_kind,
                mode=child_mode,
                parent_session_id=parent_session_id,
                parent_turn_id=parent_turn_id,
                usage=usage_stats,
            )

        task = asyncio.create_task(_run())
        return AgentExecutionHandle(
            session_id="",  # filled in via task result
            spawn_kind=spawn_kind,
            mode=child_mode,
            parent_session_id=parent_session_id,
            parent_turn_id=parent_turn_id,
            _task=task,
        )

    def _build_fresh_loop(self, parent_loop: "ALoop", *, model: str | None) -> "ALoop":
        """Build a new ALoop instance for a fresh-path child agent.

        Reuses the parent's provider/api_key but builds a new ALoop so
        per-stream state (mode_session map, token counters) is isolated.
        The child's mode (passed via stream(mode=...)) will set its own
        model. The model parameter here is only used as a fallback when
        the child mode does not define a model.
        """
        from .agent_backend import ALoop
        from .config import LoopConfig

        return ALoop(
            model=model or parent_loop._default_model_config,
            api_key=parent_loop.api_key,
            provider=parent_loop._default_provider,
            config=LoopConfig(
                max_iterations=parent_loop._default_max_iterations,
                max_session_age=parent_loop.max_session_age,
                max_session_messages=parent_loop.max_session_messages,
                compaction=parent_loop._default_compaction,
            ),
            executor=parent_loop.executor,
        )


def _generate_unique_session_id(*, max_attempts: int = 5) -> str:
    """Generate a 16-char hex session id, retrying on collision.

    A 16-char hex id has 64 bits of entropy, so collisions are extremely
    rare in practice. We retry up to max_attempts times against on-disk
    sessions to harden against the rare collision case. If every attempt
    collides, we fall back to a full 32-char uuid hex.
    """
    for _ in range(max_attempts):
        candidate = _uuid.uuid4().hex[:16]
        if AgentSession.load(candidate) is None:
            return candidate
    # Extremely unlikely fallback — use a full uuid for guaranteed uniqueness.
    return _uuid.uuid4().hex


def _write_spawn_metadata(
    session: "AgentSession",
    *,
    spawn_kind: str,
    parent_session_id: str | None,
    parent_turn_id: str | None,
    spawning_mode: str | None,
    child_mode: str | None,
) -> None:
    """Persist spawn metadata onto a child session.

    Stored as a 'spawn_metadata' dict on the session's context.json. Read
    for debugging and lineage tracking via 'aloop sessions info <id>'.
    """
    metadata = {
        "kind": spawn_kind,
        "parent_session_id": parent_session_id,
        "parent_turn_id": parent_turn_id,
        "spawning_mode": spawning_mode,
        "child_mode": child_mode,
        "timestamp": time.time(),
    }
    session.spawn_metadata = metadata
    try:
        session.save_context()
    except OSError:
        pass
