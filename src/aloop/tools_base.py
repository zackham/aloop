"""Tool definitions for the agent loop."""

import asyncio
import functools
import inspect
from dataclasses import dataclass
from typing import Annotated, Any, Awaitable, Callable, get_args, get_origin, get_type_hints


class ToolRejected(Exception):
    """Raised by on_before_tool hooks to cancel tool execution.

    When raised, the tool call is skipped and the reason is returned
    to the model as an error result.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


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
    timeout: float | None = None

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


# Sentinel for "no default provided"
class _Missing:
    """Sentinel indicating no default value was provided."""
    def __repr__(self) -> str:
        return "<MISSING>"

_MISSING = _Missing()


@dataclass(frozen=True)
class ToolParam:
    """Metadata for a tool parameter, used with Annotated type hints."""
    description: str = ""
    default: Any = _MISSING


# Type hint → JSON Schema type mapping
_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _type_to_schema(tp: type) -> dict:
    """Convert a Python type annotation to a JSON Schema fragment."""
    # Unwrap Annotated: extract the base type
    if get_origin(tp) is Annotated:
        tp = get_args(tp)[0]

    json_type = _TYPE_MAP.get(tp)
    if json_type:
        return {"type": json_type}
    # Fallback for unknown types
    return {"type": "string"}


def _get_tool_param(tp: type) -> ToolParam | None:
    """Extract ToolParam from an Annotated type, if present."""
    if get_origin(tp) is not Annotated:
        return None
    for arg in get_args(tp)[1:]:
        if isinstance(arg, ToolParam):
            return arg
    return None


def _wrap_sync(fn: Callable) -> Callable[..., Awaitable]:
    """Wrap a sync function to be async."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)
    return wrapper


def tool(
    name: str | None = None,
    description: str | None = None,
    timeout: float | None = None,
):
    """Decorator that produces a ToolDef from a function's type hints.

    Usage:
        @tool(name="query_db", description="Run a SQL query")
        async def query_db(
            sql: Annotated[str, ToolParam(description="SQL query")],
            limit: Annotated[int, ToolParam(description="Max rows")] = 100,
        ) -> str:
            ...

        # query_db is now a ToolDef
    """
    def decorator(fn: Callable) -> ToolDef:
        tool_name = name if name is not None else fn.__name__
        tool_desc = description if description is not None else (inspect.getdoc(fn) or "")

        # Get type hints (resolves forward refs)
        try:
            hints = get_type_hints(fn, include_extras=True)
        except Exception:
            hints = {}

        # Remove return annotation
        hints.pop("return", None)

        # Get function signature for defaults
        sig = inspect.signature(fn)

        properties: dict[str, dict] = {}
        required: list[str] = []

        for param_name, param in sig.parameters.items():
            # Skip _prefixed params (reserved for context injection)
            if param_name.startswith("_"):
                continue

            hint = hints.get(param_name)
            if hint is None:
                # No type hint — skip
                continue

            # Build schema for this param
            schema = _type_to_schema(hint)

            # Extract ToolParam metadata from Annotated
            tool_param = _get_tool_param(hint)
            if tool_param is not None and tool_param.description:
                schema["description"] = tool_param.description

            properties[param_name] = schema

            # Determine if required: no default = required
            has_default = param.default is not inspect.Parameter.empty
            # Also check ToolParam.default
            if not has_default and tool_param is not None and not isinstance(tool_param.default, _Missing):
                has_default = True

            if not has_default:
                required.append(param_name)

        parameters = {
            "type": "object",
            "properties": properties,
        }
        if required:
            parameters["required"] = required

        # Wrap sync functions to async
        execute_fn = fn
        if not inspect.iscoroutinefunction(fn):
            execute_fn = _wrap_sync(fn)

        return ToolDef(
            name=tool_name,
            description=tool_desc,
            parameters=parameters,
            execute=execute_fn,
            timeout=timeout,
        )

    return decorator
