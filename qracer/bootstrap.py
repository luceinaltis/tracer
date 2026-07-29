"""Composition root — build LLM and data registries from config + provider catalog.

Lives in a neutral module (not `cli.py`) so both the CLI and the web app can build
registries without a `cli ↔ web.app` import cycle. Heavy imports stay local to the
function, so importing this module is cheap.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from qracer.config.loader import load_config

if TYPE_CHECKING:
    from qracer.data.registry import DataRegistry
    from qracer.llm.registry import LLMRegistry

logger = logging.getLogger(__name__)


def build_registries() -> tuple["LLMRegistry", "DataRegistry", list[str]]:
    """Build LLM and data registries from providers.toml + provider catalog.

    Returns ``(llm_registry, data_registry, warnings)`` where *warnings*
    is a list of human-readable strings describing providers that could
    not be loaded.
    """
    import importlib

    from qracer.data.registry import DataRegistry
    from qracer.llm.providers import Role
    from qracer.llm.registry import LLMRegistry
    from qracer.provider_catalog import discover_data_providers, discover_llm_providers
    from qracer.provider_lifecycle import initialize_provider_sync

    config = load_config()
    llm_registry = LLMRegistry()
    data_registry = DataRegistry()
    warnings: list[str] = []

    # Discover providers: built-ins + any installed entry-point plugins.
    data_catalog = discover_data_providers()
    llm_catalog = discover_llm_providers()

    sorted_providers = sorted(
        config.providers.providers.items(),
        key=lambda item: item[1].priority,
    )

    for name, prov_cfg in sorted_providers:
        if not prov_cfg.enabled:
            continue

        # Resolve API key (shared by data and llm paths)
        api_key: str | None = None
        if prov_cfg.api_key_env:
            api_key = config.credentials.get(prov_cfg.api_key_env) or os.environ.get(
                prov_cfg.api_key_env
            )
            if not api_key:
                msg = f"{name}: {prov_cfg.api_key_env} not set — skipped"
                warnings.append(msg)
                logger.warning("Provider '%s' skipped: %s not set", name, prov_cfg.api_key_env)
                continue

        if prov_cfg.kind == "data" and name in data_catalog:
            adapter_path, cap_paths = data_catalog[name]
            try:
                mod_path, cls_name = adapter_path.rsplit(".", 1)
                adapter_cls = getattr(importlib.import_module(mod_path), cls_name)
                adapter = adapter_cls(api_key=api_key) if api_key else adapter_cls()
                if not initialize_provider_sync(name, adapter):
                    warnings.append(f"{name}: failed initialize/health_check — excluded")
                    continue
                caps = []
                for cp in cap_paths:
                    cp_mod, cp_name = cp.rsplit(".", 1)
                    caps.append(getattr(importlib.import_module(cp_mod), cp_name))
                data_registry.register(name, adapter, caps)
            except Exception as exc:
                msg = f"{name}: {exc}"
                warnings.append(msg)
                logger.warning("Data provider '%s' unavailable: %s", name, exc)

        elif prov_cfg.kind == "llm" and name in llm_catalog:
            adapter_path, role_values = llm_catalog[name]
            try:
                mod_path, cls_name = adapter_path.rsplit(".", 1)
                adapter_cls = getattr(importlib.import_module(mod_path), cls_name)
                adapter = adapter_cls(api_key=api_key)
                if not initialize_provider_sync(name, adapter):
                    warnings.append(f"{name}: failed initialize/health_check — excluded")
                    continue
                roles = [Role(v) for v in role_values]
                llm_registry.register(name, adapter, roles)
            except Exception as exc:
                msg = f"{name}: {exc}"
                warnings.append(msg)
                logger.warning("LLM provider '%s' unavailable: %s", name, exc)

    return llm_registry, data_registry, warnings


__all__ = ["build_registries"]
