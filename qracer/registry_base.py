"""Generic keyed registry shared by DataRegistry (capability→adapter) and
LLMRegistry (role→provider).

Both mapped a key to a priority-ordered ``list[(name, obj)]`` with identical
register / get / get-all / list-keys logic and only different error wording. This
base holds that logic; subclasses set the nouns and how a key is rendered in errors.
"""

from __future__ import annotations

from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class KeyedRegistry(Generic[K, V]):
    """A key → priority-ordered ``[(name, value), ...]`` registry with named lookup."""

    # Subclasses override these to preserve their exact error messages.
    _item_noun: str = "item"
    _key_noun: str = "key"

    def __init__(self) -> None:
        self._entries: dict[K, list[tuple[str, V]]] = {}

    def _key_label(self, key: K) -> str:
        """How a key appears in error messages (e.g. ``cap.__name__`` / ``role.value``)."""
        return str(key)

    def _register(self, name: str, value: V, keys: list[K]) -> None:
        """Register *value* under each key; earlier registrations rank higher."""
        for key in keys:
            self._entries.setdefault(key, []).append((name, value))

    def _get(self, key: K, name: str | None = None) -> V:
        """Highest-priority value for *key*, or the named one. Raises KeyError."""
        entries = self._entries.get(key)
        if not entries:
            raise KeyError(
                f"No {self._item_noun} registered for {self._key_noun} {self._key_label(key)}"
            )
        if name is not None:
            for entry_name, value in entries:
                if entry_name == name:
                    return value
            raise KeyError(
                f"No {self._item_noun} named '{name}' for {self._key_noun} {self._key_label(key)}"
            )
        return entries[0][1]

    def _get_all(self, key: K) -> list[tuple[str, V]]:
        """All (name, value) for *key* in priority order."""
        return list(self._entries.get(key, []))

    def _keys(self) -> list[K]:
        """All registered keys."""
        return list(self._entries.keys())


__all__ = ["KeyedRegistry"]
