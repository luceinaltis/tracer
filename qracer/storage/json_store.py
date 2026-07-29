"""Base class for a list of dataclass items persisted to a JSON file.

`AlertStore`, `TaskStore`, and `AgentStore` all persisted a `list[...]` to a JSON
file with byte-identical load/save/mtime-hot-reload boilerplate. This collapses
that into one generic base; subclasses supply `_serialize`/`_deserialize` and their
domain methods, and reuse `self._items` + `_save()`/`_maybe_reload()`/`_find()`.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class JsonListStore(ABC, Generic[T]):
    """A JSON-file-backed list of items with cross-process mtime hot-reload.

    Subclasses set the class attribute ``_label`` (used in log messages) and
    implement ``_serialize``/``_deserialize``. Reads that should observe external
    writes call ``self._maybe_reload()`` first (as the domain query methods do).
    """

    _label: str = "items"

    def __init__(self, path: Path) -> None:
        self._path = path
        self._mtime: float = 0.0
        self._items: list[T] = self._load()

    @abstractmethod
    def _serialize(self, item: T) -> dict[str, Any]:
        """Convert one item to a JSON-serializable dict."""

    @abstractmethod
    def _deserialize(self, data: dict[str, Any]) -> T:
        """Reconstruct one item from its stored dict."""

    # -- persistence --------------------------------------------------------

    def _maybe_reload(self) -> None:
        """Re-read from disk if another process modified the file."""
        if not self._path.exists():
            return
        try:
            current_mtime = self._path.stat().st_mtime
        except OSError:
            return
        if current_mtime != self._mtime:
            self._items = self._load()

    def _load(self) -> list[T]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._mtime = self._path.stat().st_mtime
            if isinstance(data, list):
                return [self._deserialize(item) for item in data if isinstance(item, dict)]
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            logger.warning("Failed to load %s from %s", self._label, self._path)
        return []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = [self._serialize(item) for item in self._items]
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self._mtime = self._path.stat().st_mtime

    def _find(self, item_id: str) -> T | None:
        """Return the item whose ``.id`` matches, or None."""
        return next((i for i in self._items if getattr(i, "id", None) == item_id), None)

    def __len__(self) -> int:
        return len(self._items)


__all__ = ["JsonListStore"]
