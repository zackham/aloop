"""ACP (Agent Client Protocol) server for aloop.

Wraps ALoop as an ACP agent, translating InferenceEvents
to ACP session notifications. Run via `aloop serve`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any
from uuid import uuid4

from acp import (
    Agent,
    Client,
    InitializeResponse,
    NewSessionResponse,
    LoadSessionResponse,
    PromptResponse,
    PROTOCOL_VERSION,
    run_agent,
    session_notification,
    start_tool_call,
    update_tool_call,
    update_agent_message_text,
    update_agent_thought_text,
    tool_content,
    text_block,
)
from acp.schema import (
    AgentCapabilities,
    CloseSessionResponse,
    Cost,
    ForkSessionResponse,
    Implementation,
    ListSessionsResponse,
    PromptCapabilities,
    ResumeSessionResponse,
    SessionCapabilities,
    SessionInfo,
    TextContentBlock,
    Usage,
    UsageUpdate,
)

from . import __version__
from .agent_backend import ALoop
from .compaction import get_compaction_settings
from .models import get_model
from .session import AgentSession
from .system_prompt import build_system_prompt
from .tools import ANALYSIS_TOOLS
from .tools.skills import load_skill_tool
from .types import EventType

log = logging.getLogger(__name__)



def _extract_text(prompt: list[Any]) -> str:
    """Extract plain text from ACP prompt content blocks."""
    parts: list[str] = []
    for block in prompt:
        if hasattr(block, "text"):
            parts.append(block.text)
        elif isinstance(block, dict) and "text" in block:
            parts.append(block["text"])
    return "\n".join(parts)


def _resolve_api_key() -> str:
    """Resolve OpenRouter API key from env or credential file."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        from pathlib import Path

        cred_file = Path.home() / ".aloop" / "credentials.json"
        if cred_file.exists():
            api_key = json.loads(cred_file.read_text()).get("api_key", "")
    return api_key


class _SessionState:
    """Internal state for a single ACP session."""

    __slots__ = ("session_id", "cwd", "backend", "agent_session", "cancel_event", "mode")

    def __init__(
        self,
        session_id: str,
        cwd: str,
        backend: ALoop,
        agent_session: AgentSession | None = None,
        mode: str | None = None,
    ):
        self.session_id = session_id
        self.cwd = cwd
        self.backend = backend
        self.agent_session = agent_session
        self.cancel_event = asyncio.Event()
        self.mode = mode


class AloopAgent:
    """ACP server wrapping ALoop.

    Implements the acp.Agent protocol. Each ACP session gets its own
    ALoop instance (stateful: token counters, compaction).
    """

    def __init__(self, model: str | None = None):
        self._model = model or os.environ.get("ALOOP_MODEL")
        if not self._model:
            raise ValueError(
                "No model specified. Set ALOOP_MODEL env var or pass --model to aloop."
            )
        self._sessions: dict[str, _SessionState] = {}
        self._conn: Client | None = None

    def on_connect(self, conn: Client) -> None:
        """Called by ACP SDK when the connection is established."""
        self._conn = conn

    # ── ACP Agent methods ──────────────────────────────────────────────

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any | None = None,
        client_info: Any | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_info=Implementation(
                name="aloop",
                title="aloop",
                version=__version__,
            ),
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(),
                session_capabilities=SessionCapabilities(),
            ),
        )

    async def new_session(
        self,
        cwd: str,
        mcp_servers: Any | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        session_id = str(uuid4())
        state = self._create_session_state(session_id, cwd)
        self._sessions[session_id] = state
        return NewSessionResponse(session_id=session_id)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: Any | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse | None:
        # Restore existing session or create fresh — acpx expects load to
        # always succeed so the session is available for subsequent prompts.
        state = self._create_session_state(session_id, cwd)
        existing = AgentSession.load(session_id=session_id)
        if existing is not None:
            state.agent_session = existing
        self._sessions[session_id] = state
        return LoadSessionResponse()

    async def list_sessions(
        self,
        cursor: str | None = None,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> ListSessionsResponse:
        from .session import list_sessions

        raw = list_sessions()
        sessions = []
        for entry in raw:
            sid = entry.get("session_id", "")
            sessions.append(
                SessionInfo(
                    session_id=sid,
                    cwd=cwd or os.getcwd(),
                    title=sid,
                    updated_at=None,
                )
            )
        return ListSessionsResponse(sessions=sessions)

    async def prompt(
        self,
        prompt: list[Any],
        session_id: str,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> PromptResponse:
        state = self._sessions.get(session_id)
        if state is None:
            raise ValueError(f"Unknown session: {session_id}")

        # Reset cancellation for this prompt
        state.cancel_event.clear()

        text = _extract_text(prompt)
        stop_reason = await self._stream_to_acp(state, text)

        # Build usage for the response
        usage_data = state.backend.usage
        usage = Usage(
            input_tokens=usage_data.get("input_tokens", 0),
            output_tokens=usage_data.get("output_tokens", 0),
            total_tokens=(
                usage_data.get("input_tokens", 0) + usage_data.get("output_tokens", 0)
            ),
        )

        return PromptResponse(stop_reason=stop_reason, usage=usage)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        state = self._sessions.get(session_id)
        if state is not None:
            state.cancel_event.set()

    async def fork_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: Any | None = None,
        **kwargs: Any,
    ) -> ForkSessionResponse:
        fork_turn_id = kwargs.get("fork_turn_id")

        # Try to find the source session (in-memory first, then disk)
        source: AgentSession | None = None
        mem_state = self._sessions.get(session_id)
        if mem_state and mem_state.agent_session:
            source = mem_state.agent_session
        else:
            source = AgentSession.load(session_id)

        # If source has messages with turn_ids, use real fork machinery
        if source is not None:
            resolved = source.resolve_messages()
            if resolved and any(m.get("turn_id") for m in resolved):
                if fork_turn_id is None:
                    # Fork at the last turn
                    for msg in reversed(resolved):
                        if msg.get("turn_id"):
                            fork_turn_id = msg["turn_id"]
                            break

                if fork_turn_id is not None:
                    child = AgentSession.fork(
                        parent_session_id=source.session_id,
                        fork_turn_id=fork_turn_id,
                    )
                    state = self._create_session_state(child.session_id, cwd)
                    state.agent_session = child
                    self._sessions[child.session_id] = state
                    return ForkSessionResponse(session_id=child.session_id)

        # Fallback: create a blank session (no messages or no turn_ids)
        new_id = str(uuid4())
        state = self._create_session_state(new_id, cwd)
        self._sessions[new_id] = state
        return ForkSessionResponse(session_id=new_id)

    async def resume_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: Any | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        if session_id not in self._sessions:
            # Try loading from disk
            loaded = await self.load_session(cwd, session_id)
            if loaded is None:
                return ResumeSessionResponse()
        return ResumeSessionResponse()

    async def close_session(self, session_id: str, **kwargs: Any) -> CloseSessionResponse | None:
        state = self._sessions.pop(session_id, None)
        if state is not None:
            state.cancel_event.set()
        return CloseSessionResponse()

    async def set_session_mode(self, mode_id: str, session_id: str, **kwargs: Any) -> None:
        """Change the mode for an active session.

        Loads mode config, reconfigures the session's backend
        (model, provider, compaction), and stores the mode name.
        The mode's system_prompt/system_prompt_file takes effect on next prompt.
        """
        state = self._sessions.get(session_id)
        if state is None:
            return None

        from .config import load_mode, resolve_mode_system_prompt
        from .system_prompt import _load_aloop_config
        from . import get_project_root

        project_root = get_project_root()
        project_config = _load_aloop_config(project_root)

        # Validates mode exists (raises ValueError if not)
        mode_config = load_mode(mode_id, project_config)

        # Rebuild backend if mode specifies model/provider
        api_key = _resolve_api_key()
        model_id = mode_config.get("model", self._model)
        backend = ALoop(
            model=model_id,
            api_key=api_key,
            compaction_settings=get_compaction_settings(),
        )

        # Apply mode's compaction if specified
        if "compaction" in mode_config:
            from .compaction import CompactionSettings
            mc = mode_config["compaction"]
            backend.compaction_settings = CompactionSettings(
                enabled=mc.get("enabled", True),
                reserve_tokens=mc.get("reserve_tokens", backend.compaction_settings.reserve_tokens),
                keep_recent_tokens=mc.get("keep_recent_tokens", backend.compaction_settings.keep_recent_tokens),
            )

        if "max_iterations" in mode_config:
            backend.max_iterations = mode_config["max_iterations"]
            backend.config.max_iterations = mode_config["max_iterations"]

        state.backend = backend
        state.mode = mode_id

        # Track mode on the backend's session_modes dict
        state.backend._record_session_mode(session_id, mode_id)

        return None

    async def set_session_model(self, model_id: str, session_id: str, **kwargs: Any) -> None:
        """Change the model for an active session."""
        state = self._sessions.get(session_id)
        if state is None:
            return None
        # Rebuild backend with the new model
        api_key = _resolve_api_key()
        state.backend = ALoop(
            model=model_id,
            api_key=api_key,
            compaction_settings=get_compaction_settings(),
        )
        return None

    async def set_config_option(self, config_id: str, session_id: str, value: str | bool, **kwargs: Any) -> None:
        return None

    async def authenticate(self, method_id: str, **kwargs: Any) -> None:
        return None

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        pass

    # ── Internal helpers ───────────────────────────────────────────────

    def _create_session_state(self, session_id: str, cwd: str) -> _SessionState:
        """Create a new session state with backend and tools configured."""
        # Set CWD so aloop tools resolve paths correctly
        os.environ["ALOOP_PROJECT_ROOT"] = cwd

        api_key = _resolve_api_key()
        backend = ALoop(
            model=self._model,
            api_key=api_key,
            compaction_settings=get_compaction_settings(),
        )

        agent_session = AgentSession.get_or_create(
            session_id=session_id,
        )

        return _SessionState(
            session_id=session_id,
            cwd=cwd,
            backend=backend,
            agent_session=agent_session,
        )

    async def _stream_to_acp(self, state: _SessionState, prompt_text: str) -> str:
        """Drive backend.stream(), emit ACP notifications, return stop_reason."""
        conn = self._conn
        if conn is None:
            return "end_turn"

        session_id = state.session_id
        backend = state.backend

        # Build stream kwargs
        tools = ANALYSIS_TOOLS[:]
        if not any(t.name == "load_skill" for t in tools):
            tools = tools + [load_skill_tool]

        stream_kw: dict[str, Any] = {
            "session_id": session_id,
            "tools": tools,
            "system_prompt": build_system_prompt(tools=tools),
        }

        try:
            async for event in backend.stream(prompt_text, **stream_kw):
                if state.cancel_event.is_set():
                    return "cancelled"

                match event.type:
                    case EventType.TEXT_DELTA:
                        text = event.data.get("text", "")
                        if text:
                            await conn.session_update(
                                session_id=session_id,
                                update=update_agent_message_text(text),
                            )

                    case EventType.THINKING_DELTA:
                        text = event.data.get("text", "")
                        if text:
                            await conn.session_update(
                                session_id=session_id,
                                update=update_agent_thought_text(text),
                            )

                    case EventType.TOOL_START:
                        name = event.data.get("name", "")
                        tool_call_id = event.data.get("id", "")
                        args = event.data.get("args")

                        # Map aloop tool names to ACP tool kinds
                        kind = _tool_kind(name)
                        await conn.session_update(
                            session_id=session_id,
                            update=start_tool_call(
                                tool_call_id=tool_call_id,
                                title=name,
                                kind=kind,
                                status="in_progress",
                                raw_input=args,
                            ),
                        )

                    case EventType.TOOL_END:
                        tool_call_id = event.data.get("id", "")
                        result = event.data.get("result", "")
                        is_error = event.data.get("is_error", False)
                        status = "failed" if is_error else "completed"

                        await conn.session_update(
                            session_id=session_id,
                            update=update_tool_call(
                                tool_call_id=tool_call_id,
                                status=status,
                                content=[tool_content(text_block(result[:10000]))],
                                raw_output=result,
                            ),
                        )

                    case EventType.TOOL_DELTA:
                        tool_call_id = event.data.get("id", "")
                        content = event.data.get("content", "")
                        if tool_call_id and content:
                            await conn.session_update(
                                session_id=session_id,
                                update=update_tool_call(
                                    tool_call_id=tool_call_id,
                                    status="in_progress",
                                    content=[tool_content(text_block(content))],
                                ),
                            )

                    case EventType.TURN_END:
                        # Emit per-turn usage update
                        turn_input = event.data.get("input_tokens", 0)
                        turn_output = event.data.get("output_tokens", 0)
                        turn_cost = event.data.get("cost_usd", 0) or 0
                        model_config = backend.model_config
                        await conn.session_update(
                            session_id=session_id,
                            update=UsageUpdate(
                                session_update="usage_update",
                                size=model_config.context_window,
                                used=turn_input + turn_output,
                                cost=Cost(amount=turn_cost, currency="USD"),
                            ),
                        )

                    case EventType.LOOP_END:
                        return "end_turn"

                    case EventType.COMPACTION:
                        msgs_before = event.data.get("messages_before", 0)
                        msgs_after = event.data.get("messages_after", 0)
                        tokens_saved = event.data.get("tokens_saved", 0)
                        log.info(
                            "Compaction in session %s: %d→%d messages, %d tokens saved",
                            session_id, msgs_before, msgs_after, tokens_saved,
                        )

                    case EventType.ERROR:
                        msg = event.data.get("message", "Unknown error")
                        log.error("Backend error in session %s: %s", session_id, msg)
                        return "end_turn"

                    case _:
                        # TURN_START, LOOP_START, etc. — no ACP equivalent
                        pass

        except asyncio.CancelledError:
            return "cancelled"
        except Exception as exc:
            log.exception("Error streaming in session %s", session_id)
            # Surface the error through ACP so the caller sees it
            # instead of a silent end_turn with 0 tokens
            try:
                if conn is not None:
                    await conn.session_update(
                        session_id=session_id,
                        update=update_agent_message_text(
                            f"\n\n[aloop error] {exc}",
                        ),
                    )
            except Exception:
                pass  # Best-effort; the log.exception above is the primary record
            # Also write to stderr so stepwise's stderr capture picks it up
            import sys as _sys
            print(f"aloop _stream_to_acp error: {exc}", file=_sys.stderr)
            return "end_turn"

        return "end_turn"


def _tool_kind(name: str) -> str:
    """Map aloop tool name to ACP ToolKind."""
    mapping = {
        "read_file": "read",
        "write_file": "edit",
        "edit_file": "edit",
        "bash": "execute",
        "load_skill": "other",
    }
    return mapping.get(name, "other")


async def serve_acp(model: str | None = None) -> None:
    """Run aloop as an ACP server over stdio.

    This is the main entry point called by `aloop serve`.
    Blocks until the client disconnects.
    """
    agent = AloopAgent(model=model)
    await run_agent(agent)
