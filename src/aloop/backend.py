"""Backend protocol - the contract both Claude and AgentLoop implement."""

from typing import AsyncIterator, Protocol, runtime_checkable

from .types import InferenceEvent, InferenceResult


@runtime_checkable
class InferenceBackend(Protocol):
    async def stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        tools: list | None = None,
        **kwargs,
    ) -> AsyncIterator[InferenceEvent]:
        """Primary interface. Yields InferenceEvents, ending with COMPLETE or ERROR."""
        ...

    async def run(self, prompt: str, **kwargs) -> InferenceResult:
        """Convenience: consume stream, return final result."""
        ...
