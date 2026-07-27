"""FastAPI application factory for the qracer web dashboard API."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from qracer.agents_store import AgentStore
from qracer.alerts import AlertStore
from qracer.config.loader import _user_dir
from qracer.memory.fact_store import FactStore
from qracer.tasks import TaskStore
from qracer.watchlist import Watchlist
from qracer.web.routes import router as api_router

if TYPE_CHECKING:
    from fastapi import FastAPI


def create_app(user_dir: Path | None = None) -> "FastAPI":
    """Build a FastAPI app wired to the user's qracer state files.

    Args:
        user_dir: Optional override for the qracer config/state directory.
            Defaults to ``~/.qracer/``. Useful for tests that need an isolated
            state directory.

    Returns:
        A FastAPI app with the ``/api`` routes mounted.

    Raises:
        ImportError: If FastAPI is not installed. Install the optional ``web``
            extra: ``pip install 'qracer[web]'``.
    """
    try:
        from fastapi import FastAPI
    except ImportError as exc:
        raise ImportError(
            "qracer.web requires FastAPI. Install with: pip install 'qracer[web]'"
        ) from exc

    base_dir = user_dir if user_dir is not None else _user_dir()

    app = FastAPI(title="qracer", version="0.1.0")

    # Shared, file-backed stores. Each store hot-reloads on mtime change so
    # mutations made by `qracer repl` or `qracer serve` are picked up.
    app.state.user_dir = base_dir
    app.state.task_store = TaskStore(base_dir / "tasks.json")
    app.state.alert_store = AlertStore(base_dir / "alerts.json")
    app.state.watchlist = Watchlist(base_dir / "watchlist.json")
    app.state.agent_store = AgentStore(base_dir / "agents.json")
    app.state.fact_store = FactStore(base_dir / "fact_store.duckdb")

    # Registries give the web layer live-price + LLM access (for the dashboard and
    # briefing). Failure must not break the JSON API, so degrade to None + warn.
    try:
        from qracer.bootstrap import build_registries

        llm_registry, data_registry, _warnings = build_registries()
    except Exception:  # pragma: no cover - defensive: API stays up without registries
        logging.getLogger(__name__).warning("Failed to build registries", exc_info=True)
        llm_registry, data_registry = None, None
    app.state.llm_registry = llm_registry
    app.state.data_registry = data_registry

    app.include_router(api_router, prefix="/api")

    # Mount the NiceGUI dashboard at "/" (optional dependency).
    try:
        from qracer.web.dashboard import mount as mount_dashboard

        mount_dashboard(app, base_dir, data_registry, llm_registry, app.state.fact_store)
    except ImportError:
        pass  # nicegui not installed — API still works, no UI
    except Exception:  # pragma: no cover - never let the UI break the API
        logging.getLogger(__name__).warning("Failed to mount dashboard", exc_info=True)

    return app
