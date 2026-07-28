"""Root conftest — makes the tests/ directory importable and provides shared fixtures.

Fixtures here DRY setup that was previously hand-rolled per test file: an isolated
config directory (honored by both the loader and the writer via ``QRACER_CONFIG_DIR``)
and the fake data/LLM registries from :mod:`tests.helpers`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An isolated ``~/.qracer``-style dir wired via ``QRACER_CONFIG_DIR``.

    Both the loader (reads) and the writer (writes) resolve to this directory, and
    the loader's mtime-cached singleton is reset so each test sees a clean load.
    """
    d = tmp_path / ".qracer"
    d.mkdir()
    monkeypatch.setenv("QRACER_CONFIG_DIR", str(d))

    from qracer.config import loader

    monkeypatch.setattr(loader, "_cached_config", None, raising=False)
    monkeypatch.setattr(loader, "_config_mtimes", {}, raising=False)
    yield d


@pytest.fixture
def data_registry():
    """A DataRegistry preloaded with the fake price/fundamental/macro/news adapters."""
    from tests.helpers import make_data_registry

    return make_data_registry()


@pytest.fixture
def llm_registry():
    """An LLMRegistry backed by a FakeLLM (returns ``[]`` by default)."""
    from tests.helpers import make_llm_registry

    registry, _fake = make_llm_registry()
    return registry
