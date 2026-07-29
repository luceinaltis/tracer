"""DataRegistry — capability-based routing with fallback.

Adapters register capabilities. Agents request data by capability, not by source.
If the primary adapter fails or is unavailable, the registry falls through to fallbacks.
"""

from __future__ import annotations

import logging
from typing import Any

from qracer.registry_base import KeyedRegistry

# Provider protocol classes are used as capability keys (e.g. PriceProvider, NewsProvider).
# pyright doesn't support type[Protocol], so we use type[Any] as the capability type.
ProviderType = type[Any]

logger = logging.getLogger(__name__)


class DataRegistry(KeyedRegistry[ProviderType, Any]):
    """Routes data requests by capability with ordered fallback."""

    _item_noun = "adapter"
    _key_noun = "capability"

    def _key_label(self, key: ProviderType) -> str:
        return key.__name__

    def register(self, name: str, adapter: Any, capabilities: list[ProviderType]) -> None:
        """Register an adapter with its capabilities (earlier = higher priority)."""
        self._register(name, adapter, capabilities)

    def get(self, capability: ProviderType, name: str | None = None) -> Any:
        """Get the highest-priority adapter for a capability (or a named one).

        A simple lookup — no methods invoked. For call-level fallback use
        :meth:`get_with_fallback` / :meth:`async_get_with_fallback`.

        Raises:
            KeyError: If no adapter is registered for the capability (or name).
        """
        return self._get(capability, name)

    def get_all(self, capability: ProviderType) -> list[tuple[str, Any]]:
        """Get all adapters for a capability in priority order."""
        return self._get_all(capability)

    def capabilities(self) -> list[ProviderType]:
        """List all registered capabilities."""
        return self._keys()

    def _require(self, capability: ProviderType) -> list[tuple[str, Any]]:
        """Adapters for *capability*, or raise the standard 'not registered' KeyError."""
        adapters = self._get_all(capability)
        if not adapters:
            noun, key_noun, label = self._item_noun, self._key_noun, self._key_label(capability)
            raise KeyError(f"No {noun} registered for {key_noun} {label}")
        return adapters

    def get_with_fallback(
        self, capability: ProviderType, method: str, *args: Any, **kwargs: Any
    ) -> Any:
        """Call *method* on each adapter for *capability* until one succeeds.

        Raises:
            KeyError: If no adapter is registered for the capability.
            Exception: The last exception if all adapters fail.
        """
        last_exc: Exception | None = None
        for adapter_name, adapter in self._require(capability):
            try:
                return getattr(adapter, method)(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                logger.warning("Adapter '%s'.%s failed, trying next: %s", adapter_name, method, exc)
        raise last_exc  # type: ignore[misc]

    async def async_get_with_fallback(
        self, capability: ProviderType, method: str, *args: Any, **kwargs: Any
    ) -> Any:
        """Async twin of :meth:`get_with_fallback` — awaits each adapter method."""
        last_exc: Exception | None = None
        for adapter_name, adapter in self._require(capability):
            try:
                return await getattr(adapter, method)(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                logger.warning("Adapter '%s'.%s failed, trying next: %s", adapter_name, method, exc)
        raise last_exc  # type: ignore[misc]
