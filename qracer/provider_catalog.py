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
}

# All known data-provider capability protocols (for runtime isinstance checks).
_DATA_CAPABILITY_PROTOCOLS: list[tuple[str, str]] = [
    ("qracer.data.providers", "PriceProvider"),
    ("qracer.data.providers", "FundamentalProvider"),
    ("qracer.data.providers", "MacroProvider"),
    ("qracer.data.providers", "NewsProvider"),
    ("qracer.data.providers", "AlternativeProvider"),
    ("qracer.data.providers", "StreamingProvider"),
]


def discover_data_providers() -> dict[str, tuple[str, list[str]]]:
    """Discover data providers from entry points and merge with built-ins.

    Scans the ``qracer.data_providers`` entry-point group.  Each entry point
    should reference an adapter class.  Capabilities are detected by checking
    the adapter class against the runtime-checkable data-provider protocols.

    Returns a merged catalog in the same format as
    :data:`BUILTIN_DATA_PROVIDERS`.
    """
    merged = dict(BUILTIN_DATA_PROVIDERS)

    eps = importlib.metadata.entry_points(group="qracer.data_providers")
    for ep in eps:
        try:
            adapter_cls = ep.load()
        except Exception as exc:
            logger.warning("Failed to load data-provider entry point '%s': %s", ep.name, exc)
            continue

        # Detect which capability protocols this adapter satisfies.
        caps: list[str] = []
        for mod_path, cls_name in _DATA_CAPABILITY_PROTOCOLS:
            try:
                mod = importlib.import_module(mod_path)
                protocol_cls = getattr(mod, cls_name)
                if isinstance(adapter_cls(), protocol_cls):
                    caps.append(f"{mod_path}.{cls_name}")
            except Exception:
                continue

        if not caps:
            logger.warning(
                "Data-provider entry point '%s' does not satisfy any known "
                "capability protocol — skipped",
                ep.name,
            )
            continue

        adapter_import_path = f"{adapter_cls.__module__}.{adapter_cls.__qualname__}"
        merged[ep.name] = (adapter_import_path, caps)
        logger.info("Discovered external data provider '%s' with capabilities %s", ep.name, caps)

    return merged


def discover_llm_providers() -> dict[str, tuple[str, list[str]]]:
    """Discover LLM providers from entry points and merge with built-ins.

    Scans the ``qracer.llm_providers`` entry-point group.  Each entry point
    should reference an adapter class that has a ``roles`` class attribute
    (a list of :class:`~qracer.llm.providers.Role` values).

    Returns a merged catalog in the same format as
    :data:`BUILTIN_LLM_PROVIDERS`.
    """
    merged = dict(BUILTIN_LLM_PROVIDERS)

    eps = importlib.metadata.entry_points(group="qracer.llm_providers")
    for ep in eps:
        try:
            adapter_cls = ep.load()
        except Exception as exc:
            logger.warning("Failed to load LLM-provider entry point '%s': %s", ep.name, exc)
            continue

        # Extract roles from the adapter class attribute.
        raw_roles = getattr(adapter_cls, "roles", None)
        if not raw_roles:
            logger.warning(
                "LLM-provider entry point '%s' has no 'roles' class attribute — skipped",
                ep.name,
            )
            continue

        role_values = [r.value if hasattr(r, "value") else str(r) for r in raw_roles]
        adapter_import_path = f"{adapter_cls.__module__}.{adapter_cls.__qualname__}"
        merged[ep.name] = (adapter_import_path, role_values)
        logger.info("Discovered external LLM provider '%s' with roles %s", ep.name, role_values)

    return merged
