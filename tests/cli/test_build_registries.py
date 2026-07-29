"""Tests for CLI internal helpers."""

from __future__ import annotations

from qracer.cli import _build_registries


class TestBuildRegistries:
    def test_returns_registry_tuple(self) -> None:
        """Should return (LLMRegistry, DataRegistry, warnings) without crashing."""
        llm, data, warnings = _build_registries()
        assert llm is not None
        assert data is not None
        assert isinstance(warnings, list)
