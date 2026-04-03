"""Bash tool for inference agents."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..tools_base import ToolDef, ToolResult

from .. import get_project_root
MAX_OUTPUT = 50_000
MAX_STDERR = 10_000


bash_tool = ToolDef(
    name="bash",
    description="Execute a shell command in the project root.",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command"},
            "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 300},
        },
        "required": ["command"],
    },
    execute=None,
)


async def _bash(command: str, timeout: int = 300) -> ToolResult:
    timeout = max(1, int(timeout))
    proc = None
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(get_project_root()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        output = stdout.decode(errors="replace")[:MAX_OUTPUT]
        err = stderr.decode(errors="replace")[:MAX_STDERR]
        if err:
            output = f"{output}\nSTDERR:\n{err}" if output else f"STDERR:\n{err}"

        if proc.returncode != 0:
            output = f"{output}\nExit code: {proc.returncode}".strip()
            return ToolResult(content=output or f"Exit code: {proc.returncode}", is_error=True)

        return ToolResult(content=output or "(no output)")

    except asyncio.TimeoutError:
        if proc is not None:
            proc.kill()
            await proc.communicate()
        return ToolResult(content=f"Command timed out after {timeout}s", is_error=True)
    except Exception as exc:  # pragma: no cover - defensive
        return ToolResult(content=f"Command error: {exc}", is_error=True)
    finally:
        # Kill subprocess on CancelledError or any other BaseException
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass


bash_tool.execute = _bash
