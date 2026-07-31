"""Built-in provider catalog — maps provider names to adapter classes.

New providers only need an entry here and in providers.toml.
The registry builder uses this catalog for dynamic import and registration.

External packages can also register providers via entry points::

    [project.entry-points."qracer.data_providers"]
    polygon = "qracer_polygon.adapter:PolygonAdapter"

    [project.entry-points."qracer.llm_providers"]
    custom_llm = "my_pkg.adapter:MyLLMAdapter"
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

# Data providers: name -> (adapter import path, [capability import paths])
BUILTIN_DATA_PROVIDERS: dict[str, tuple[str, list[str]]] = {
    "yfinance": (
        "qracer.data.yfinance_adapter.YfinanceAdapter",
        ["qracer.data.providers.PriceProvider"],
    ),
    "finnhub": (
        "qracer.data.finnhub_adapter.FinnhubAdapter",
        [
            "qracer.data.providers.FundamentalProvider",
            "qracer.data.providers.NewsProvider",
            "qracer.data.providers.AlternativeProvider",
        ],
    ),
    "fred": (
        "qracer.data.fred_adapter.FredAdapter",
        ["qracer.data.providers.MacroProvider"],
    ),
    "dart": (
        "qracer.data.dart_adapter.DartAdapter",
        [
            "qracer.data.providers.FundamentalProvider",
            "qracer.data.providers.NewsProvider",
            "qracer.data.providers.AlternativeProvider",
        ],
    ),
    "kis": (
        "qracer.data.kis_adapter.KisAdapter",
        [
            "qracer.data.providers.PriceProvider",
            "qracer.data.providers.DerivativesProvider",
        ],
    ),
}

# LLM providers: name -> (adapter import path, [role enum values])
BUILTIN_LLM_PROVIDERS: dict[str, tuple[str, list[str]]] = {
    "claude": (
        "qracer.llm.claude_adapter.ClaudeAdapter",
        [
            "researcher",
            "analyst",
            "strategist",
            "reporter",
        ],
    ),
    "openai": (
        "qracer.llm.openai_adapter.OpenAIAdapter",
        [
            "researcher",
            "analyst",
            "strategist",
            "reporter",
        ],
    ),
    "gemini": (
        "qracer.llm.gemini_adapter.GeminiAdapter",
        [
            "researcher",
            "analyst",
            "strategist",
            "reporter",
        ],
    ),
    "openrouter": (
        "qracer.llm.openrouter_adapter.OpenRouterAdapter",
        [
            "researcher",
            "analyst",
            "strategist",
            "reporter",
        ],
    ),
}

# All known data-provider capability protocols (for runtime isinstance checks).
_DATA_CAPABILITY_PROTOCOLS: list[tuple[str, str]] = [
    ("qracer.data.providers", "PriceProvider"),
    ("qracer.data.providers", "FundamentalProvider"),
    ("qracer.data.providers", "MacroProvider"),
    ("qracer.data.providers", "NewsProvider"),
    ("qracer.data.providers", "AlternativeProvider"),
    ("qracer.data.providers", "DerivativesProvider"),
    ("qracer.data.providers", "StreamingProvider"),
]


def _discover(
    group: str,
    builtins: dict[str, tuple[str, list[str]]],
    kind: str,
    extract_meta: Callable[[type, str], list[str]],
) -> dict[str, tuple[str, list[str]]]:
    """Merge *builtins* with providers found on the *group* entry-point group.

    ``extract_meta(adapter_cls, ep_name)`` returns the metadata list (capability
    paths or role values); an empty list means "skip" and the extractor is
    responsible for logging why.
    """
    merged = dict(builtins)
    for ep in importlib.metadata.entry_points(group=group):
        try:
            adapter_cls = ep.load()
        except Exception as exc:
            logger.warning("Failed to load %s-provider entry point '%s': %s", kind, ep.name, exc)
            continue
        meta = extract_meta(adapter_cls, ep.name)
        if not meta:
            continue  # extract_meta logged the skip reason
        adapter_import_path = f"{adapter_cls.__module__}.{adapter_cls.__qualname__}"
        merged[ep.name] = (adapter_import_path, meta)
        logger.info("Discovered external %s provider '%s': %s", kind, ep.name, meta)
    return merged


def _data_capabilities(adapter_cls: type, ep_name: str) -> list[str]:
    """Capability protocol paths an adapter class satisfies (empty → skip)."""
    caps: list[str] = []
    for mod_path, cls_name in _DATA_CAPABILITY_PROTOCOLS:
        try:
            protocol_cls = getattr(importlib.import_module(mod_path), cls_name)
            if isinstance(adapter_cls(), protocol_cls):
                caps.append(f"{mod_path}.{cls_name}")
        except Exception:
            continue
    if not caps:
        logger.warning(
            "Data-provider entry point '%s' does not satisfy any known "
            "capability protocol — skipped",
            ep_name,
        )
    return caps


def _llm_roles(adapter_cls: type, ep_name: str) -> list[str]:
    """Role values from an adapter class's ``roles`` attribute (empty → skip)."""
    raw_roles = getattr(adapter_cls, "roles", None)
    if not raw_roles:
        logger.warning(
            "LLM-provider entry point '%s' has no 'roles' class attribute — skipped", ep_name
        )
        return []
    return [r.value if hasattr(r, "value") else str(r) for r in raw_roles]


def discover_data_providers() -> dict[str, tuple[str, list[str]]]:
    """Discover data providers from the ``qracer.data_providers`` entry-point group,
    merged with the built-ins. Capabilities are detected against the runtime-checkable
    data-provider protocols."""
    return _discover("qracer.data_providers", BUILTIN_DATA_PROVIDERS, "data", _data_capabilities)


def discover_llm_providers() -> dict[str, tuple[str, list[str]]]:
    """Discover LLM providers from the ``qracer.llm_providers`` entry-point group,
    merged with the built-ins. Roles come from each adapter class's ``roles`` attribute."""
    return _discover("qracer.llm_providers", BUILTIN_LLM_PROVIDERS, "LLM", _llm_roles)
