"""Tool definitions for the agent loop."""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass
class ToolResult:
    content: str
    details: dict | None = None
    is_error: bool = False


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict
    execute: Callable[..., ToolResult | Awaitable[ToolResult] | Any]

    def to_schema(self) -> dict:
        """Convert to OpenAI function-calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def tool(name: str, description: str, parameters: dict):
    """Decorator to create a ToolDef from a function."""

    def decorator(fn):
        fn._tool_def = ToolDef(
            name=name,
            description=description,
            parameters=parameters,
            execute=fn,
        )
        return fn

    return decorator
