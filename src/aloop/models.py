"""Model registry — user config from ~/.aloop/models.json, with sensible defaults.

Any OpenRouter model ID (e.g. 'anthropic/claude-sonnet-4-20250514') works
out of the box. The registry adds short aliases and cost tracking metadata.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path


def _config_path() -> Path:
    return Path.home() / ".aloop" / "models.json"


# Default context window and cost for unknown models (conservative estimates)
_DEFAULT_CONTEXT = 128_000
_DEFAULT_MAX_OUTPUT = 16_384
_DEFAULT_COST_INPUT = 1.0
_DEFAULT_COST_OUTPUT = 3.0


@dataclass(frozen=True)
class ModelConfig:
    id: str
    name: str
    context_window: int = _DEFAULT_CONTEXT
    max_output: int = _DEFAULT_MAX_OUTPUT
    cost_input: float = _DEFAULT_COST_INPUT
    cost_output: float = _DEFAULT_COST_OUTPUT
    supports_tools: bool = True
    supports_images: bool = False
    provider_order: tuple[str, ...] | None = None
    tier: str = "background"
    stream_timeout: float = 60.0
    enabled: bool = True


# Built-in short aliases. Users extend via ~/.aloop/models.json.
DEFAULT_MODELS: dict[str, ModelConfig] = {}


_models: dict[str, ModelConfig] | None = None
MODELS: dict[str, ModelConfig] = {}


def _normalize(raw: dict) -> dict:
    normalized = dict(raw)
    provider_order = normalized.get("provider_order")
    if isinstance(provider_order, list):
        normalized["provider_order"] = tuple(provider_order)
    return normalized


def _persist_models(models: dict[str, ModelConfig]) -> None:
    user_models: dict[str, dict] = {}

    for key, config in models.items():
        if key not in DEFAULT_MODELS or config != DEFAULT_MODELS[key]:
            user_models[key] = asdict(config)

    _config_path().parent.mkdir(parents=True, exist_ok=True)
    _config_path().write_text(json.dumps({"models": user_models}, indent=2), encoding="utf-8")


def _load_models() -> dict[str, ModelConfig]:
    global _models, MODELS

    models = dict(DEFAULT_MODELS)
    if _config_path().exists():
        try:
            data = json.loads(_config_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        for key, raw in data.get("models", {}).items():
            if not isinstance(raw, dict):
                continue
            try:
                models[key] = ModelConfig(**_normalize(raw))
            except TypeError:
                continue

    _models = models
    MODELS.clear()
    MODELS.update(models)
    return models


def get_models() -> dict[str, ModelConfig]:
    if _models is None:
        _load_models()
    return _models or {}


def get_model(key: str) -> ModelConfig:
    """Get a model config by key.

    If the key matches a registered model (built-in or user-configured),
    returns that config. Otherwise, treats the key as a raw OpenRouter
    model ID and creates a default config for it.
    """
    models = get_models()
    if key in models:
        return models[key]

    # Treat as a raw OpenRouter model ID (e.g. 'anthropic/claude-sonnet-4-20250514')
    return ModelConfig(
        id=key,
        name=key,
    )


def list_models() -> list[dict]:
    return [{"key": key, **asdict(config)} for key, config in get_models().items()]


def save_model(key: str, config: ModelConfig) -> None:
    models = dict(get_models())
    models[key] = config
    _persist_models(models)
    _load_models()


def delete_model(key: str) -> bool:
    if key in DEFAULT_MODELS:
        return False

    models = dict(get_models())
    if key not in models:
        return False

    del models[key]
    _persist_models(models)
    _load_models()
    return True
