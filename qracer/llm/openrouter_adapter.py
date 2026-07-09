"""OpenRouterAdapter — LLM provider for OpenRouter's unified model gateway.

OpenRouter exposes an OpenAI-compatible API, so this adapter reuses the OpenAI
client with a custom ``base_url``.  Unlike the single-vendor adapters, the model
is **not** hard-coded: OpenRouter serves hundreds of models, so the model is
chosen by the user (persisted in ``config.toml`` as ``llm_model``) and applied to
every role.

The public ``/models`` endpoint needs no API key, so :func:`list_models` works
with the standard library alone.  It backs both the ``qracer install`` model
picker and the ``qracer models`` command.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass

from qracer.llm.providers import (
    CompletionRequest,
    CompletionResponse,
    Role,
)

try:
    import openai  # pyright: ignore[reportMissingImports]

    _HAS_OPENAI = True
except ImportError:
    _HAS_OPENAI = False

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODELS_URL = f"{OPENROUTER_BASE_URL}/models"

# Widely-available fallback used when the user has not selected a model yet.
DEFAULT_OPENROUTER_MODEL = "openai/gpt-4o-mini"


@dataclass(frozen=True)
class ModelInfo:
    """An OpenRouter model, trimmed to the fields we display."""

    id: str
    name: str
    context_length: int
    prompt_price: float  # USD per input token
    completion_price: float  # USD per output token

    @property
    def prompt_per_million(self) -> float:
        """Input price per 1M tokens (USD)."""
        return self.prompt_price * 1_000_000

    @property
    def completion_per_million(self) -> float:
        """Output price per 1M tokens (USD)."""
        return self.completion_price * 1_000_000


def _to_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _parse_model(raw: dict) -> ModelInfo:
    pricing = raw.get("pricing") or {}
    model_id = raw.get("id", "")
    return ModelInfo(
        id=model_id,
        name=raw.get("name") or model_id,
        context_length=int(raw.get("context_length") or 0),
        prompt_price=_to_float(pricing.get("prompt")),
        completion_price=_to_float(pricing.get("completion")),
    )


def list_models(search: str | None = None, *, timeout: float = 15.0) -> list[ModelInfo]:
    """Fetch available OpenRouter models from the public ``/models`` endpoint.

    No API key is required.  Uses the standard library only, so this works even
    when the optional ``openai`` dependency is not installed.

    Args:
        search: optional case-insensitive substring matched against model id or
            name.  ``None`` or empty returns everything.
        timeout: network timeout in seconds.

    Returns:
        Models sorted by id.

    Raises:
        OSError / urllib.error.URLError on network failure — callers decide how
        to degrade.
    """
    req = urllib.request.Request(
        OPENROUTER_MODELS_URL,
        headers={"Accept": "application/json", "User-Agent": "qracer"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted URL)
        payload = json.loads(resp.read().decode("utf-8"))

    models = [_parse_model(m) for m in payload.get("data", [])]
    if search:
        needle = search.lower()
        models = [m for m in models if needle in m.id.lower() or needle in m.name.lower()]
    return sorted(models, key=lambda m: m.id)


def _resolve_configured_model() -> str:
    """Return the user-selected OpenRouter model from config, or the default.

    Reads ``config.app.llm_model`` only when ``llm_provider == "openrouter"`` so
    a Claude/OpenAI model id left in config is never sent to OpenRouter.
    """
    try:
        from qracer.config.loader import load_config

        cfg = load_config()
        if cfg.app.llm_provider == "openrouter" and cfg.app.llm_model:
            return cfg.app.llm_model
    except Exception:  # pragma: no cover - defensive: never block on config
        pass
    return DEFAULT_OPENROUTER_MODEL


class OpenRouterAdapter:
    """LLM adapter routing every role through a single OpenRouter model."""

    roles: list[Role] = list(Role)

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        model_map: dict[Role, str] | None = None,
    ) -> None:
        if not _HAS_OPENAI:
            raise ImportError(
                "openai is not installed. Install it with: uv sync --extra openrouter"
            )
        chosen = model or _resolve_configured_model()
        self._model = chosen
        self._model_map = model_map or {role: chosen for role in Role}
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=OPENROUTER_BASE_URL,
            default_headers={"HTTP-Referer": "https://github.com/", "X-Title": "qracer"},
        )

    def model_for_role(self, role: Role) -> str:
        """Get the model assigned to a role (a single model for OpenRouter)."""
        return self._model_map.get(role, self._model)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Send a completion request through OpenRouter (OpenAI-compatible)."""
        model = request.model or self.model_for_role(Role.RESEARCHER)

        messages: list[dict[str, str]] = [
            {"role": msg.role, "content": msg.content} for msg in request.messages
        ]

        response = await self._client.chat.completions.create(
            model=model,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            messages=messages,  # type: ignore[arg-type]
        )

        content = ""
        if response.choices:
            content = response.choices[0].message.content or ""

        input_tokens = response.usage.prompt_tokens if response.usage else 0
        output_tokens = response.usage.completion_tokens if response.usage else 0

        return CompletionResponse(
            content=content,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            # OpenRouter pricing varies per model and is returned out-of-band;
            # left unestimated here (browse prices via `qracer models`).
            cost=0.0,
        )
