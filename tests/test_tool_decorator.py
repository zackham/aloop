"""Tests for the @tool decorator and ToolParam."""

import asyncio
import inspect
from typing import Annotated

import pytest

from aloop.tools_base import ToolDef, ToolResult, ToolParam, tool, _MISSING, _Missing


# ── ToolParam basics ──────────────────────────────────────────────


class TestToolParam:
    def test_default_values(self):
        tp = ToolParam()
        assert tp.description == ""
        assert isinstance(tp.default, _Missing)

    def test_with_description(self):
        tp = ToolParam(description="A query string")
        assert tp.description == "A query string"

    def test_with_explicit_default(self):
        tp = ToolParam(description="limit", default=100)
        assert tp.default == 100

    def test_missing_sentinel_repr(self):
        assert repr(_MISSING) == "<MISSING>"


# ── Basic decorator usage ─────────────────────────────────────────


class TestToolDecoratorBasic:
    def test_returns_tooldef(self):
        @tool()
        async def my_tool(x: str) -> str:
            return x

        assert isinstance(my_tool, ToolDef)

    def test_name_from_function(self):
        @tool()
        async def search_docs(query: str) -> str:
            return query

        assert search_docs.name == "search_docs"

    def test_name_from_decorator_arg(self):
        @tool(name="custom_name")
        async def search_docs(query: str) -> str:
            return query

        assert search_docs.name == "custom_name"

    def test_description_from_docstring(self):
        @tool()
        async def search_docs(query: str) -> str:
            """Search the documentation."""
            return query

        assert search_docs.description == "Search the documentation."

    def test_description_from_decorator_arg(self):
        @tool(description="Custom description")
        async def search_docs(query: str) -> str:
            """This docstring is ignored."""
            return query

        assert search_docs.description == "Custom description"

    def test_no_docstring_empty_description(self):
        @tool()
        async def search_docs(query: str) -> str:
            return query

        assert search_docs.description == ""


# ── Schema generation per type ────────────────────────────────────


class TestSchemaTypes:
    def test_str_type(self):
        @tool()
        async def fn(x: str) -> str:
            return x

        assert fn.parameters["properties"]["x"] == {"type": "string"}

    def test_int_type(self):
        @tool()
        async def fn(x: int) -> str:
            return str(x)

        assert fn.parameters["properties"]["x"] == {"type": "integer"}

    def test_float_type(self):
        @tool()
        async def fn(x: float) -> str:
            return str(x)

        assert fn.parameters["properties"]["x"] == {"type": "number"}

    def test_bool_type(self):
        @tool()
        async def fn(x: bool) -> str:
            return str(x)

        assert fn.parameters["properties"]["x"] == {"type": "boolean"}

    def test_list_type(self):
        @tool()
        async def fn(x: list) -> str:
            return str(x)

        assert fn.parameters["properties"]["x"] == {"type": "array"}

    def test_dict_type(self):
        @tool()
        async def fn(x: dict) -> str:
            return str(x)

        assert fn.parameters["properties"]["x"] == {"type": "object"}

    def test_unknown_type_fallback(self):
        """Unknown types fall back to string."""

        class Custom:
            pass

        @tool()
        async def fn(x: Custom) -> str:
            return str(x)

        assert fn.parameters["properties"]["x"] == {"type": "string"}


# ── Annotated with ToolParam ──────────────────────────────────────


class TestAnnotated:
    def test_annotated_description(self):
        @tool()
        async def fn(
            query: Annotated[str, ToolParam(description="Search query")],
        ) -> str:
            return query

        prop = fn.parameters["properties"]["query"]
        assert prop == {"type": "string", "description": "Search query"}

    def test_annotated_without_toolparam(self):
        """Annotated with non-ToolParam metadata is just the base type."""

        @tool()
        async def fn(x: Annotated[int, "some metadata"]) -> str:
            return str(x)

        assert fn.parameters["properties"]["x"] == {"type": "integer"}

    def test_multiple_params_with_annotations(self):
        @tool(name="query_db", description="Run a SQL query")
        async def query_db(
            sql: Annotated[str, ToolParam(description="SQL query")],
            limit: Annotated[int, ToolParam(description="Max rows")] = 100,
        ) -> str:
            return sql

        props = query_db.parameters["properties"]
        assert props["sql"] == {"type": "string", "description": "SQL query"}
        assert props["limit"] == {"type": "integer", "description": "Max rows"}


# ── Required vs optional ─────────────────────────────────────────


class TestRequired:
    def test_no_default_is_required(self):
        @tool()
        async def fn(x: str) -> str:
            return x

        assert fn.parameters["required"] == ["x"]

    def test_with_default_is_optional(self):
        @tool()
        async def fn(x: str = "hello") -> str:
            return x

        assert "required" not in fn.parameters or fn.parameters.get("required") == []

    def test_mixed_required_optional(self):
        @tool()
        async def fn(
            required_param: str,
            optional_param: int = 10,
        ) -> str:
            return required_param

        assert fn.parameters["required"] == ["required_param"]
        assert "required_param" in fn.parameters["properties"]
        assert "optional_param" in fn.parameters["properties"]

    def test_toolparam_default_makes_optional(self):
        """ToolParam with explicit default makes the parameter optional."""

        @tool()
        async def fn(
            x: Annotated[int, ToolParam(description="count", default=5)],
        ) -> str:
            return str(x)

        # x has no Python default but ToolParam has default → optional
        assert "required" not in fn.parameters or "x" not in fn.parameters.get("required", [])

    def test_all_optional_no_required_key(self):
        @tool()
        async def fn(x: str = "a", y: int = 1) -> str:
            return x

        # required list should be absent or empty
        assert fn.parameters.get("required", []) == []


# ── _prefixed params skipped ──────────────────────────────────────


class TestPrefixedParams:
    def test_underscore_prefix_skipped(self):
        @tool()
        async def fn(
            query: str,
            _context: dict = None,
        ) -> str:
            return query

        assert "query" in fn.parameters["properties"]
        assert "_context" not in fn.parameters["properties"]

    def test_only_underscore_params(self):
        @tool()
        async def fn(_ctx: dict) -> str:
            return ""

        assert fn.parameters["properties"] == {}

    def test_multiple_underscore_params(self):
        @tool()
        async def fn(
            name: str,
            _ctx: dict = None,
            _session: str = None,
        ) -> str:
            return name

        assert list(fn.parameters["properties"].keys()) == ["name"]


# ── Sync auto-wrapping ───────────────────────────────────────────


class TestSyncWrapping:
    def test_sync_function_wrapped_to_async(self):
        @tool()
        def my_sync_tool(x: str) -> str:
            return x.upper()

        assert isinstance(my_sync_tool, ToolDef)
        assert inspect.iscoroutinefunction(my_sync_tool.execute)

    def test_async_function_stays_async(self):
        @tool()
        async def my_async_tool(x: str) -> str:
            return x.upper()

        assert inspect.iscoroutinefunction(my_async_tool.execute)

    async def test_sync_wrapped_executes(self):
        @tool()
        def add(a: int, b: int) -> int:
            return a + b

        result = await add.execute(a=3, b=4)
        assert result == 7

    async def test_async_executes(self):
        @tool()
        async def greet(name: str) -> str:
            return f"hello {name}"

        result = await greet.execute(name="world")
        assert result == "hello world"


# ── Timeout field ─────────────────────────────────────────────────


class TestTimeout:
    def test_default_timeout_is_none(self):
        @tool()
        async def fn(x: str) -> str:
            return x

        assert fn.timeout is None

    def test_timeout_from_decorator(self):
        @tool(timeout=30.0)
        async def fn(x: str) -> str:
            return x

        assert fn.timeout == 30.0

    def test_timeout_on_tooldef_directly(self):
        td = ToolDef(
            name="test",
            description="test",
            parameters={},
            execute=lambda: None,
            timeout=60.0,
        )
        assert td.timeout == 60.0

    def test_timeout_default_on_tooldef(self):
        td = ToolDef(
            name="test",
            description="test",
            parameters={},
            execute=lambda: None,
        )
        assert td.timeout is None


# ── Parameters structure ──────────────────────────────────────────


class TestParametersStructure:
    def test_parameters_is_json_schema_object(self):
        @tool()
        async def fn(x: str) -> str:
            return x

        assert fn.parameters["type"] == "object"
        assert "properties" in fn.parameters

    def test_to_schema_still_works(self):
        @tool(name="my_tool", description="Does stuff")
        async def fn(x: str) -> str:
            return x

        schema = fn.to_schema()
        assert schema == {
            "type": "function",
            "function": {
                "name": "my_tool",
                "description": "Does stuff",
                "parameters": {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                    "required": ["x"],
                },
            },
        }


# ── No type hint params skipped ──────────────────────────────────


class TestNoHints:
    def test_param_without_hint_skipped(self):
        @tool()
        async def fn(x: str, y) -> str:  # noqa: ANN001
            return x

        assert "x" in fn.parameters["properties"]
        assert "y" not in fn.parameters["properties"]


# ── Return annotation ignored ─────────────────────────────────────


class TestReturnAnnotation:
    def test_return_not_in_properties(self):
        @tool()
        async def fn(x: str) -> ToolResult:
            return ToolResult(content=x)

        assert "return" not in fn.parameters["properties"]


# ── Full integration example ──────────────────────────────────────


class TestIntegration:
    async def test_full_tool_lifecycle(self):
        """End-to-end: define, inspect schema, execute."""

        @tool(name="query_db", description="Run a SQL query", timeout=10.0)
        async def query_db(
            sql: Annotated[str, ToolParam(description="The SQL query to run")],
            limit: Annotated[int, ToolParam(description="Maximum rows to return")] = 100,
            _context: dict = None,
        ) -> ToolResult:
            return ToolResult(content=f"Ran: {sql} (limit={limit})")

        # It's a ToolDef
        assert isinstance(query_db, ToolDef)
        assert query_db.name == "query_db"
        assert query_db.description == "Run a SQL query"
        assert query_db.timeout == 10.0

        # Schema is correct
        assert query_db.parameters["required"] == ["sql"]
        assert "limit" not in query_db.parameters["required"]
        assert "_context" not in query_db.parameters["properties"]

        props = query_db.parameters["properties"]
        assert props["sql"]["description"] == "The SQL query to run"
        assert props["limit"]["type"] == "integer"

        # It executes
        result = await query_db.execute(sql="SELECT 1", limit=50)
        assert isinstance(result, ToolResult)
        assert "SELECT 1" in result.content
        assert "limit=50" in result.content

    def test_importable_from_aloop(self):
        """tool and ToolParam are importable from the top-level package."""
        from aloop import tool as t, ToolParam as tp

        assert t is tool
        assert tp is ToolParam
