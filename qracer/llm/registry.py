"""LLMRegistry — role-based routing with per-role overrides.

Adapters register for roles. Agents request completions by role, not by provider.
Per-role assignment is overridable via config.
"""

from __future__ import annotations

from qracer.llm.providers import LLMProvider, Role
from qracer.registry_base import KeyedRegistry


class LLMRegistry(KeyedRegistry[Role, LLMProvider]):
    """Routes LLM requests by role with ordered fallback."""

    _item_noun = "provider"
    _key_noun = "role"

    def _key_label(self, key: Role) -> str:
        return key.value

    def register(self, name: str, provider: LLMProvider, roles: list[Role]) -> None:
        """Register a provider for the given roles (earlier = higher priority)."""
        self._register(name, provider, roles)

    def get(self, role: Role, name: str | None = None) -> LLMProvider:
        """Get a provider by role, optionally by explicit name. Raises KeyError."""
        return self._get(role, name)

    def get_all(self, role: Role) -> list[tuple[str, LLMProvider]]:
        """Get all providers for a role in priority order."""
        return self._get_all(role)

    def roles(self) -> list[Role]:
        """List all registered roles."""
        return self._keys()
