"""Provider registry — known API endpoints for LLM inference.

Each provider is an OpenAI-compatible chat completions endpoint.
OpenRouter is the default. Users can override via --provider flag
or default_provider in ~/.aloop/config.json.

Custom providers can be added via ~/.aloop/providers.json.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ProviderConfig:
    """Configuration for an API provider."""
    name: str
    base_url: str
    env_key: str  # environment variable for the API key
    status: str = "tested"  # "tested" or "community"
    extra_headers: dict[str, str] = field(default_factory=dict)
    # OpenRouter-specific: provider routing
    supports_provider_routing: bool = False
    notes: str = ""


# Built-in providers. Status "tested" means we've validated streaming,
# tool calling, and multi-turn with this endpoint.
PROVIDERS: dict[str, ProviderConfig] = {
    "openrouter": ProviderConfig(
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1/chat/completions",
        env_key="OPENROUTER_API_KEY",
        status="tested",
        extra_headers={
            "HTTP-Referer": "https://github.com/zackham/aloop",
            "X-Title": "aloop",
        },
        supports_provider_routing=True,
        notes="Any model on the platform. Model IDs use provider/model format.",
    ),
    "openai": ProviderConfig(
        name="OpenAI",
        base_url="https://api.openai.com/v1/chat/completions",
        env_key="OPENAI_API_KEY",
        status="tested",
        notes="Direct OpenAI API. Model IDs: gpt-4o, gpt-4o-mini, etc.",
    ),
    "anthropic": ProviderConfig(
        name="Anthropic (OpenAI-compat)",
        base_url="https://api.anthropic.com/v1/chat/completions",
        env_key="ANTHROPIC_API_KEY",
        status="tested",
        notes="Anthropic's OpenAI-compatible endpoint. Model IDs: claude-sonnet-4-20250514, etc.",
    ),
    "google": ProviderConfig(
        name="Google AI (Gemini)",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        env_key="GOOGLE_API_KEY",
        status="tested",
        notes="Google AI Studio / Gemini API (OpenAI-compat). Model IDs: gemini-2.5-flash, gemini-2.5-pro, etc.",
    ),
    "groq": ProviderConfig(
        name="Groq",
        base_url="https://api.groq.com/openai/v1/chat/completions",
        env_key="GROQ_API_KEY",
        status="tested",
        notes="Fast inference. Model IDs: llama-3.3-70b-versatile, meta-llama/llama-4-scout-17b-16e-instruct, etc.",
    ),
    "together": ProviderConfig(
        name="Together AI",
        base_url="https://api.together.xyz/v1/chat/completions",
        env_key="TOGETHER_API_KEY",
        status="community",
        notes="Open-source models. Model IDs: meta-llama/Llama-3-70b-chat-hf, etc.",
    ),
    "ollama": ProviderConfig(
        name="Ollama (local)",
        base_url="http://localhost:11434/v1/chat/completions",
        env_key="",  # no key needed
        status="community",
        notes="Local models via Ollama. No API key required. Model IDs: llama3, codellama, etc.",
    ),
}

DEFAULT_PROVIDER = "openrouter"


def _user_config_path() -> Path:
    return Path.home() / ".aloop" / "providers.json"


def _load_user_providers() -> dict[str, ProviderConfig]:
    """Load custom providers from ~/.aloop/providers.json."""
    path = _user_config_path()
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}

    custom: dict[str, ProviderConfig] = {}
    for key, raw in data.items():
        if not isinstance(raw, dict) or "base_url" not in raw:
            continue
        custom[key] = ProviderConfig(
            name=raw.get("name", key),
            base_url=raw["base_url"],
            env_key=raw.get("env_key", ""),
            status="community",
            extra_headers=raw.get("extra_headers", {}),
            notes=raw.get("notes", "User-configured provider."),
        )
    return custom


def get_providers() -> dict[str, ProviderConfig]:
    """All providers: built-in + user-configured."""
    merged = dict(PROVIDERS)
    merged.update(_load_user_providers())
    return merged


def get_provider(name: str) -> ProviderConfig:
    """Get a provider by name. Raises KeyError if not found."""
    providers = get_providers()
    if name not in providers:
        available = ", ".join(sorted(providers.keys()))
        raise KeyError(f"Unknown provider: {name}. Available: {available}")
    return providers[name]


def get_default_provider_name() -> str:
    """Read default provider from ~/.aloop/config.json, or return 'openrouter'."""
    config_path = Path.home() / ".aloop" / "config.json"
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
            name = data.get("provider")
            if isinstance(name, str) and name.strip():
                return name.strip()
        except (OSError, json.JSONDecodeError):
            pass
    return DEFAULT_PROVIDER
