"""Inference event types - the unified streaming protocol."""

from dataclasses import dataclass, field
from enum import Enum
import time


class EventType(str, Enum):
    TEXT_DELTA = "text_delta"
    THINKING_START = "thinking_start"
    THINKING_DELTA = "thinking_delta"
    THINKING_END = "thinking_end"
    TOOL_START = "tool_start"
    TOOL_DELTA = "tool_delta"
    TOOL_END = "tool_end"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    LOOP_START = "loop_start"
    LOOP_END = "loop_end"
    COMPACTION = "compaction"
    ERROR = "error"

    # Deprecated alias — use LOOP_END
    COMPLETE = "loop_end"


class InferenceError(Exception):
    pass


class ModeConflictError(Exception):
    """Raised when stream() is called with a different mode on an existing session."""
    pass


@dataclass
class InferenceEvent:
    """A streaming event from either backend."""

    type: EventType
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    session_id: str | None = None
    turn_id: str | None = None
    tool_call_id: str | None = None

    @staticmethod
    def text(delta: str) -> "InferenceEvent":
        return InferenceEvent(EventType.TEXT_DELTA, {"text": delta})

    @staticmethod
    def thinking(delta: str) -> "InferenceEvent":
        return InferenceEvent(EventType.THINKING_DELTA, {"text": delta})

    @staticmethod
    def thinking_start() -> "InferenceEvent":
        return InferenceEvent(EventType.THINKING_START, {})

    @staticmethod
    def thinking_end() -> "InferenceEvent":
        return InferenceEvent(EventType.THINKING_END, {})

    @staticmethod
    def tool_start(
        name: str,
        tool_call_id: str,
        args: dict | None = None,
    ) -> "InferenceEvent":
        return InferenceEvent(
            EventType.TOOL_START,
            {"name": name, "id": tool_call_id, "args": args},
            tool_call_id=tool_call_id,
        )

    @staticmethod
    def tool_end(
        name: str,
        tool_call_id: str,
        result: str,
        is_error: bool = False,
    ) -> "InferenceEvent":
        return InferenceEvent(
            EventType.TOOL_END,
            {
                "name": name,
                "id": tool_call_id,
                "result": result,
                "is_error": is_error,
            },
            tool_call_id=tool_call_id,
        )

    @staticmethod
    def loop_start(
        session_id: str | None = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> "InferenceEvent":
        return InferenceEvent(
            EventType.LOOP_START,
            {
                "session_id": session_id,
                "model": model,
                "provider": provider,
            },
            session_id=session_id,
        )

    @staticmethod
    def loop_end(
        text: str,
        session_id: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float | None = None,
        model: str | None = None,
        turns: int = 0,
    ) -> "InferenceEvent":
        return InferenceEvent(
            EventType.LOOP_END,
            {
                "text": text,
                "session_id": session_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
                "model": model,
                "turns": turns,
            },
            session_id=session_id,
        )

    @staticmethod
    def compaction(
        messages_before: int,
        messages_after: int,
        tokens_saved: int,
    ) -> "InferenceEvent":
        return InferenceEvent(
            EventType.COMPACTION,
            {
                "messages_before": messages_before,
                "messages_after": messages_after,
                "tokens_saved": tokens_saved,
            },
        )

    @staticmethod
    def complete(
        text: str,
        session_id: str | None = None,
        cost_usd: float | None = None,
        usage: dict | None = None,
    ) -> "InferenceEvent":
        """Deprecated — use loop_end() instead."""
        return InferenceEvent(
            EventType.LOOP_END,
            {
                "text": text,
                "session_id": session_id,
                "cost_usd": cost_usd,
                "usage": usage,
            },
            session_id=session_id,
        )

    @staticmethod
    def error(message: str) -> "InferenceEvent":
        return InferenceEvent(EventType.ERROR, {"message": message})


@dataclass
class RunResult:
    """Final result from ALoop.run()."""

    text: str
    session_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    model: str | None = None
    turns: int = 0


# Deprecated alias — use RunResult instead
InferenceResult = RunResult
