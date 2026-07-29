"""Tests for the OpenRouter adapter and model catalog."""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

import qracer.llm.openrouter_adapter as ora
from qracer.llm.openrouter_adapter import (
    DEFAULT_OPENROUTER_MODEL,
    ModelInfo,
    OpenRouterAdapter,
    _resolve_configured_model,
    list_models,
)

_URLOPEN = "qracer.llm.openrouter_adapter.urllib.request.urlopen"

_SAMPLE = {
    "data": [
        {
            "id": "openai/gpt-4o",
            "name": "GPT-4o",
            "context_length": 128000,
            "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
        },
        {
            "id": "anthropic/claude-3.5-sonnet",
            "name": "Claude 3.5 Sonnet",
            "context_length": 200000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        },
        {
            "id": "meta-llama/llama-3-8b",
            "name": "Llama 3 8B",
            "context_length": 8192,
            "pricing": {"prompt": "0", "completion": "0"},
        },
    ]
}


def _mock_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# list_models
# ---------------------------------------------------------------------------


class TestListModels:
    def test_parses_and_sorts_by_id(self) -> None:
        with patch(_URLOPEN, return_value=_mock_response(_SAMPLE)):
            result = list_models()

        ids = [m.id for m in result]
        assert ids == sorted(ids)
        gpt = next(m for m in result if m.id == "openai/gpt-4o")
        assert gpt.name == "GPT-4o"
        assert gpt.context_length == 128000
        assert gpt.prompt_per_million == pytest.approx(2.5)
        assert gpt.completion_per_million == pytest.approx(10.0)

    def test_search_filters_by_id(self) -> None:
        with patch(_URLOPEN, return_value=_mock_response(_SAMPLE)):
            result = list_models("claude")
        assert [m.id for m in result] == ["anthropic/claude-3.5-sonnet"]

    def test_search_matches_name_case_insensitive(self) -> None:
        with patch(_URLOPEN, return_value=_mock_response(_SAMPLE)):
            result = list_models("LLAMA")
        assert len(result) == 1
        assert result[0].id == "meta-llama/llama-3-8b"

    def test_handles_missing_optional_fields(self) -> None:
        with patch(_URLOPEN, return_value=_mock_response({"data": [{"id": "x/y"}]})):
            result = list_models()
        assert result[0] == ModelInfo(
            id="x/y", name="x/y", context_length=0, prompt_price=0.0, completion_price=0.0
        )

    def test_network_error_propagates(self) -> None:
        with patch(_URLOPEN, side_effect=urllib.error.URLError("down")):
            with pytest.raises(urllib.error.URLError):
                list_models()


# ---------------------------------------------------------------------------
# _resolve_configured_model
# ---------------------------------------------------------------------------


def _config(provider: str, model: str):
    from qracer.config.models import AppConfig, QracerConfig

    return QracerConfig(app=AppConfig(llm_provider=provider, llm_model=model))


class TestResolveConfiguredModel:
    def test_uses_config_model_when_provider_is_openrouter(self) -> None:
        with patch(
            "qracer.config.loader.load_config",
            return_value=_config("openrouter", "x-ai/grok-4.5"),
        ):
            assert _resolve_configured_model() == "x-ai/grok-4.5"

    def test_defaults_when_provider_is_not_openrouter(self) -> None:
        with patch(
            "qracer.config.loader.load_config",
            return_value=_config("claude", "claude-sonnet-4-20250514"),
        ):
            assert _resolve_configured_model() == DEFAULT_OPENROUTER_MODEL

    def test_defaults_when_config_unavailable(self) -> None:
        with patch("qracer.config.loader.load_config", side_effect=RuntimeError("boom")):
            assert _resolve_configured_model() == DEFAULT_OPENROUTER_MODEL


# ---------------------------------------------------------------------------
# OpenRouterAdapter
# ---------------------------------------------------------------------------


class TestOpenRouterAdapter:
    def test_requires_openai_dependency(self) -> None:
        with patch.object(ora, "_HAS_OPENAI", False):
            with pytest.raises(ImportError, match="openai"):
                OpenRouterAdapter(api_key="k")

    def test_single_model_applied_to_all_roles(self) -> None:
        from qracer.llm.providers import Role

        fake_openai = MagicMock()
        with (
            patch.object(ora, "_HAS_OPENAI", True),
            patch.object(ora, "openai", fake_openai, create=True),
        ):
            adapter = OpenRouterAdapter(api_key="k", model="anthropic/claude-3.5-sonnet")

        for role in Role:
            assert adapter.model_for_role(role) == "anthropic/claude-3.5-sonnet"
        # base_url points at OpenRouter.
        _, kwargs = fake_openai.AsyncOpenAI.call_args
        assert kwargs["base_url"] == ora.OPENROUTER_BASE_URL


# ---------------------------------------------------------------------------
# Catalog registration
# ---------------------------------------------------------------------------


def test_openrouter_registered_in_builtin_catalog() -> None:
    from qracer.provider_catalog import BUILTIN_LLM_PROVIDERS

    assert "openrouter" in BUILTIN_LLM_PROVIDERS
    path, roles = BUILTIN_LLM_PROVIDERS["openrouter"]
    assert path.endswith("OpenRouterAdapter")
    assert set(roles) == {"researcher", "analyst", "strategist", "reporter"}
