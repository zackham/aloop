"""Live integration test for ALoop.complete() — hits a real provider.

Gated behind OPENROUTER_API_KEY so CI and unprivileged runs skip cleanly.
Run locally with:

    OPENROUTER_API_KEY=sk-or-v1-... uv run pytest tests/test_complete_live.py -xvs

Kept intentionally narrow: one happy-path call against a cheap model.
All error paths, provider routing, and parameter passthrough are
covered by mocks in test_complete.py / test_complete_providers.py.
"""

from __future__ import annotations

import os

import pytest

from aloop import ALoop


pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"),
    reason="OPENROUTER_API_KEY not set — skipping live integration test",
)


async def test_complete_hits_openrouter():
    """Real round-trip against OpenRouter with a cheap model.

    Assertions are lenient on content (no exact text match) but strict
    on the observable side-effects of a successful inference call:
    non-empty text, positive token counts, positive cost, turns == 1,
    and a populated model id.
    """
    aloop = ALoop(
        model="google/gemini-3.1-flash-lite-preview",
        provider="openrouter",
        api_key=os.environ["OPENROUTER_API_KEY"],
    )

    result = await aloop.complete(
        "Reply with exactly: ok",
        max_tokens=10,
    )

    assert result.text, f"expected non-empty text, got {result.text!r}"
    assert result.input_tokens > 0, f"expected input_tokens > 0, got {result.input_tokens}"
    assert result.output_tokens > 0, f"expected output_tokens > 0, got {result.output_tokens}"
    assert result.cost_usd is not None and result.cost_usd > 0, (
        f"expected cost_usd > 0, got {result.cost_usd}"
    )
    assert result.turns == 1
    assert result.model, f"expected populated model id, got {result.model!r}"
