"""Unit tests for non-render helpers in ``qracer.web.ui``.

The NiceGUI render closures require a browser client and are smoke-covered by the
``GET /`` test; here we cover the pure model-catalog helper, including its
network-failure fallback.
"""

from __future__ import annotations

import pytest

pytest.importorskip("nicegui")

from qracer.web import ui as web_ui  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_model_cache() -> None:
    web_ui._model_cache = None


class TestModelOptions:
    def test_returns_model_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        monkeypatch.setattr(
            web_ui,
            "list_models",
            lambda *a, **k: [SimpleNamespace(id="x/y"), SimpleNamespace(id="a/b")],
        )
        assert web_ui._model_options() == ["x/y", "a/b"]

    def test_caches_after_first_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        calls = {"n": 0}

        def fake_list(*a: object, **k: object):
            calls["n"] += 1
            return [SimpleNamespace(id="x/y")]

        monkeypatch.setattr(web_ui, "list_models", fake_list)
        web_ui._model_options()
        web_ui._model_options()  # second call served from cache
        assert calls["n"] == 1

    def test_network_failure_returns_empty_and_does_not_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*a: object, **k: object):
            raise RuntimeError("no network")

        monkeypatch.setattr(web_ui, "list_models", boom)
        assert web_ui._model_options() == []
        # A transient failure must not be cached (so the next render retries).
        assert web_ui._model_cache is None
