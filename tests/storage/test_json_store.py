"""Contract tests for the JsonListStore base (round-trip + hot-reload)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qracer.storage.json_store import JsonListStore


@dataclass
class _Item:
    id: str
    value: int


class _ItemStore(JsonListStore[_Item]):
    _label = "items"

    def _serialize(self, item: _Item) -> dict[str, Any]:
        return {"id": item.id, "value": item.value}

    def _deserialize(self, data: dict[str, Any]) -> _Item:
        return _Item(id=data["id"], value=int(data["value"]))

    def add(self, item: _Item) -> None:
        self._items.append(item)
        self._save()


class TestJsonListStore:
    def test_round_trip_persists_and_reloads(self, tmp_path: Path) -> None:
        path = tmp_path / "items.json"
        store = _ItemStore(path)
        store.add(_Item("a", 1))
        store.add(_Item("b", 2))
        # A fresh store over the same file reads them back.
        reloaded = _ItemStore(path)
        assert len(reloaded) == 2
        assert reloaded._find("b") == _Item("b", 2)

    def test_hot_reload_on_external_write(self, tmp_path: Path) -> None:
        path = tmp_path / "items.json"
        store = _ItemStore(path)
        store.add(_Item("a", 1))
        # Another writer replaces the file's contents.
        other = _ItemStore(path)
        other.add(_Item("z", 9))
        store._maybe_reload()
        assert store._find("z") == _Item("z", 9)

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert len(_ItemStore(tmp_path / "nope.json")) == 0

    def test_corrupt_file_degrades_to_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "items.json"
        path.write_text("not json", encoding="utf-8")
        assert len(_ItemStore(path)) == 0
