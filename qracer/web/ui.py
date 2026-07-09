"""NiceGUI configuration + results UI for custom agents.

Mounted onto the existing FastAPI app (same port) via :func:`mount`. The page reads
and writes the file-backed :class:`~qracer.agents_store.AgentStore` directly (no REST
round-trip), and lists selectable models from OpenRouter's public catalog. Execution
happens in ``qracer serve`` / ``qracer run``; this page is for configuring agents and
checking their latest results.

Kept deliberately small for a POC — it's structured so cards/tabs can grow later.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from qracer.agents_store import AgentStore, TriggerType, validate_cron
from qracer.llm.openrouter_adapter import list_models

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

_TRIGGERS = [t.value for t in TriggerType]

# Model catalog is fetched once and reused across renders (network call).
_model_cache: list[str] | None = None


def _model_options() -> list[str]:
    global _model_cache
    if _model_cache is None:
        try:
            _model_cache = [m.id for m in list_models()]
        except OSError:
            logger.warning("Could not fetch OpenRouter model catalog")
            _model_cache = []
    return _model_cache


def mount(app: "FastAPI", base_dir: Path) -> None:
    """Register the NiceGUI agent config page and attach it to *app*."""
    from nicegui import ui

    store = AgentStore(base_dir / "agents.json")

    @ui.refreshable
    def agent_cards() -> None:
        models = _model_options()  # cached; fetched on first render, not at mount
        agents = store.agents
        if not agents:
            ui.label("No agents yet. Click “Add agent” to create one.").classes(
                "text-gray-500"
            )
        for agent in agents:
            with ui.card().classes("w-full"):
                name = ui.input("Name", value=agent.name).classes("w-full")
                if models:
                    model = ui.select(
                        models, value=agent.model, label="Model", with_input=True
                    ).classes("w-full")
                else:
                    model = ui.input("Model (OpenRouter id)", value=agent.model).classes(
                        "w-full"
                    )
                prompt = ui.textarea("Prompt", value=agent.prompt).classes("w-full")

                with ui.row().classes("items-center gap-4 w-full"):
                    trigger = ui.select(
                        _TRIGGERS, value=agent.trigger_type.value, label="Trigger"
                    )
                    cron = ui.input("cron", value=agent.cron or "").props("dense")
                    cron.bind_visibility_from(
                        trigger, "value", lambda v: v == TriggerType.CRON.value
                    )
                    enabled = ui.switch("Enabled", value=agent.enabled)

                with ui.row().classes("gap-2"):

                    def save(a=agent, name=name, model=model, prompt=prompt,
                             trigger=trigger, cron=cron, enabled=enabled) -> None:
                        trig = trigger.value
                        cron_val = cron.value.strip() or None
                        if trig == TriggerType.CRON.value and not validate_cron(cron_val or ""):
                            ui.notify(f"Invalid cron: {cron_val!r}", type="negative")
                            return
                        try:
                            store.update(
                                a.id,
                                name=name.value,
                                model=model.value,
                                prompt=prompt.value,
                                trigger_type=TriggerType(trig),
                                cron=cron_val,
                                enabled=enabled.value,
                            )
                        except ValueError as exc:
                            ui.notify(str(exc), type="negative")
                            return
                        ui.notify(f"Saved {name.value}", type="positive")
                        agent_cards.refresh()

                    def delete(a=agent) -> None:
                        store.remove(a.id)
                        ui.notify("Deleted", type="warning")
                        agent_cards.refresh()

                    ui.button("Save", on_click=save).props("color=primary")
                    ui.button("Delete", on_click=delete).props("flat color=negative")

    def add_agent() -> None:
        models = _model_options()
        default_model = models[0] if models else ""
        store.create("New agent", default_model, "You are a helpful assistant.")
        agent_cards.refresh()

    @ui.refreshable
    def result_rows() -> None:
        agents = store.agents
        if not agents:
            ui.label("No agents yet.").classes("text-gray-500")
        for agent in agents:
            with ui.card().classes("w-full"):
                ui.label(agent.describe()).classes("font-bold")
                with ui.row().classes("gap-6 text-sm text-gray-500"):
                    ui.label(f"runs: {agent.run_count}")
                    ui.label(f"last: {agent.last_run_at or '—'}")
                    ui.label(f"next: {agent.next_run_at or '—'}")
                if agent.last_error:
                    ui.label(f"error: {agent.last_error}").classes("text-red-500")
                elif agent.last_output:
                    ui.markdown(agent.last_output)
                else:
                    ui.label("(not run yet)").classes("text-gray-400")

    @ui.page("/")
    def index() -> None:
        ui.label("qracer — Custom Agents").classes("text-2xl font-bold")
        with ui.tabs() as tabs:
            config_tab = ui.tab("Agents")
            results_tab = ui.tab("Results")
        with ui.tab_panels(tabs, value=config_tab).classes("w-full"):
            with ui.tab_panel(config_tab):
                ui.button("Add agent", on_click=add_agent).props("color=primary")
                agent_cards()
            with ui.tab_panel(results_tab):
                ui.button("Refresh", on_click=result_rows.refresh).props("flat")
                result_rows()

    ui.run_with(app, storage_secret="qracer-agents")


__all__ = ["mount"]
