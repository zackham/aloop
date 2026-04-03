"""ACP (Agent Client Protocol) server for aloop.

Wraps AgentLoopBackend as an ACP agent, translating InferenceEvents
to ACP session notifications. Run via `aloop --acp`.
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
from .agent_backend import AgentLoopBackend
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

    __slots__ = ("session_id", "cwd", "backend", "agent_session", "cancel_event")

    def __init__(
        self,
        session_id: str,
        cwd: str,
        backend: AgentLoopBackend,
        agent_session: AgentSession | None = None,
    ):
        self.session_id = session_id
        self.cwd = cwd
        self.backend = backend
        self.agent_session = agent_session
        self.cancel_event = asyncio.Event()


class AloopAgent:
    """ACP server wrapping AgentLoopBackend.

    Implements the acp.Agent protocol. Each ACP session gets its own
    AgentLoopBackend instance (stateful: token counters, compaction).
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
        return None

    async def set_session_model(self, model_id: str, session_id: str, **kwargs: Any) -> None:
        """Change the model for an active session."""
        state = self._sessions.get(session_id)
        if state is None:
            return None
        # Rebuild backend with the new model
        api_key = _resolve_api_key()
        state.backend = AgentLoopBackend(
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
        backend = AgentLoopBackend(
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
            "session_key": session_id,
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

                    case EventType.COMPLETE:
                        usage_data = event.data.get("usage") or {}
                        input_tokens = usage_data.get("input_tokens", 0)
                        output_tokens = usage_data.get("output_tokens", 0)
                        cost_usd = usage_data.get("cost_usd", 0)
                        model_config = backend.model_config

                        await conn.session_update(
                            session_id=session_id,
                            update=UsageUpdate(
                                session_update="usage_update",
                                size=model_config.context_window,
                                used=input_tokens + output_tokens,
                                cost=Cost(amount=cost_usd, currency="USD"),
                            ),
                        )
                        return "end_turn"

                    case EventType.ERROR:
                        msg = event.data.get("message", "Unknown error")
                        log.error("Backend error in session %s: %s", session_id, msg)
                        return "end_turn"

                    case _:
                        # TURN_START, TURN_END, etc. — no ACP equivalent
                        pass

        except asyncio.CancelledError:
            return "cancelled"
        except Exception:
            log.exception("Error streaming in session %s", session_id)
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

    This is the main entry point called by `aloop --acp`.
    Blocks until the client disconnects.
    """
    agent = AloopAgent(model=model)
    await run_agent(agent)
