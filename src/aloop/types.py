"""Inference event types - the unified streaming protocol."""

from dataclasses import dataclass, field
from enum import Enum


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
    COMPLETE = "complete"
    ERROR = "error"


class InferenceError(Exception):
    pass


@dataclass
class InferenceEvent:
    """A streaming event from either backend."""

    type: EventType
    data: dict = field(default_factory=dict)

    @staticmethod
    def text(delta: str) -> "InferenceEvent":
        return InferenceEvent(EventType.TEXT_DELTA, {"text": delta})

    @staticmethod
    def thinking(delta: str) -> "InferenceEvent":
        return InferenceEvent(EventType.THINKING_DELTA, {"text": delta})

    @staticmethod
    def tool_start(
        name: str,
        tool_call_id: str,
        args: dict | None = None,
    ) -> "InferenceEvent":
        return InferenceEvent(
            EventType.TOOL_START,
            {"name": name, "id": tool_call_id, "args": args},
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
        )

    @staticmethod
    def complete(
        text: str,
        session_id: str | None = None,
        cost_usd: float | None = None,
        usage: dict | None = None,
    ) -> "InferenceEvent":
        return InferenceEvent(
            EventType.COMPLETE,
            {
                "text": text,
                "session_id": session_id,
                "cost_usd": cost_usd,
                "usage": usage,
            },
        )

    @staticmethod
    def error(message: str) -> "InferenceEvent":
        return InferenceEvent(EventType.ERROR, {"message": message})


@dataclass
class InferenceResult:
    """Final result extracted from a COMPLETE event."""

    text: str
    session_id: str | None = None
    cost_usd: float | None = None
    usage: dict | None = None
