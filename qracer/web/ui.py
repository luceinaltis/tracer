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

from qracer.agents_store import AgentStore, TriggerType, validate_cron
from qracer.llm.openrouter_adapter import list_models

logger = logging.getLogger(__name__)

_TRIGGERS = [t.value for t in TriggerType]

# Model catalog is fetched once and reused across renders (network call).
_model_cache: list[str] | None = None


def _model_options() -> list[str]:
    global _model_cache
    if _model_cache is None:
        try:
            _model_cache = [m.id for m in list_models()]
        except Exception:  # noqa: BLE001 - network/JSON/HTML errors all degrade the same way
            # Don't cache a transient failure: leave the cache empty so the next
            # render retries. Callers fall back to a free-text model input.
            logger.warning("Could not fetch OpenRouter model catalog", exc_info=True)
            return []
    return _model_cache


def render_agents_section(base_dir: Path) -> None:
    """Render the custom-agent config + results UI into the current NiceGUI context.

    This is the one editable dashboard section: it reads and writes the file-backed
    :class:`AgentStore` directly (no REST round-trip). Called by the dashboard page.
    """
    from nicegui import ui

    store = AgentStore(base_dir / "agents.json")

    @ui.refreshable
    def agent_cards() -> None:
        models = _model_options()  # cached; fetched on first render, not at mount
        agents = store.agents
        if not agents:
            ui.label("No agents yet. Click “Add agent” to create one.").classes("text-gray-500")
        for agent in agents:
            with ui.card().classes("w-full"):
                name = ui.input("Name", value=agent.name).classes("w-full")
                if models:
                    model = ui.select(
                        models, value=agent.model, label="Model", with_input=True
                    ).classes("w-full")
                else:
                    model = ui.input("Model (OpenRouter id)", value=agent.model).classes("w-full")
                prompt = ui.textarea("Prompt", value=agent.prompt).classes("w-full")

                with ui.row().classes("items-center gap-4 w-full"):
                    trigger = ui.select(_TRIGGERS, value=agent.trigger_type.value, label="Trigger")
                    cron = ui.input("cron", value=agent.cron or "").props("dense")
                    cron.bind_visibility_from(
                        trigger, "value", lambda v: v == TriggerType.CRON.value
                    )
                    enabled = ui.switch("Enabled", value=agent.enabled)

                with ui.row().classes("gap-2"):

                    def save(
                        a=agent,
                        name=name,
                        model=model,
                        prompt=prompt,
                        trigger=trigger,
                        cron=cron,
                        enabled=enabled,
                    ) -> None:
                        trig = trigger.value
                        cron_val = (cron.value or "").strip() or None
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

    with ui.tabs() as tabs:
        config_tab = ui.tab("Config")
        results_tab = ui.tab("Results")
    with ui.tab_panels(tabs, value=config_tab).classes("w-full"):
        with ui.tab_panel(config_tab):
            ui.button("Add agent", on_click=add_agent).props("color=primary")
            agent_cards()
        with ui.tab_panel(results_tab):
            ui.button("Refresh", on_click=result_rows.refresh).props("flat")
            result_rows()


__all__ = ["render_agents_section"]
