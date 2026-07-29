"""Tests for the registry composition root (qracer.bootstrap)."""

from __future__ import annotations

from pathlib import Path

import pytest

from qracer.bootstrap import build_registries
from qracer.data.registry import DataRegistry
from qracer.llm.registry import LLMRegistry


def test_build_registries_returns_triple(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolated empty config dir → no providers → empty registries, no crash.
    monkeypatch.setenv("QRACER_CONFIG_DIR", str(tmp_path))
    from qracer.config.loader import load_config

    load_config(force_reload=True)
    try:
        llm_registry, data_registry, warnings = build_registries()
        assert isinstance(llm_registry, LLMRegistry)
        assert isinstance(data_registry, DataRegistry)
        assert isinstance(warnings, list)
    finally:
        # Reset the module-level config cache for other tests.
        monkeypatch.delenv("QRACER_CONFIG_DIR", raising=False)
        load_config(force_reload=True)
