"""NiceGUI Settings section — the editable config surface for the web dashboard.

Renders the declarative settings registry (:mod:`qracer.config.settings_schema`) as
grouped forms plus a Providers group, and writes changes through the structured
config writer (:mod:`qracer.config.writer`). This is the web twin of ``qracer config``:
both front-ends share the same schema and the same write path.

Mirrors the in-process pattern of :func:`qracer.web.ui.render_agents_section` — no REST
round-trip. API keys are entered masked (``password``) and are write-only: an existing
key is shown as "set", never echoed back.
"""

from __future__ import annotations

from pathlib import Path

from qracer.config import settings_schema as ss
from qracer.config import writer
from qracer.config.loader import load_config

# ---------------------------------------------------------------------------
# Pure apply helpers (no NiceGUI) — unit-tested directly
# ---------------------------------------------------------------------------


def _coerce_widget(setting: ss.Setting, value: object) -> object:
    """Coerce a widget value to the setting's type. Raises ValueError."""
    kind = setting.kind
    if kind == "int":
        return int(value)  # type: ignore[arg-type]
    if kind == "float":
        return float(value)  # type: ignore[arg-type]
    if kind == "bool":
        return bool(value)
    return "" if value is None else str(value)


def apply_scalar(setting: ss.Setting, value: object, config_dir: Path | None = None) -> object:
    """Validate + persist one scalar setting; returns the stored value. Raises ValueError."""
    coerced = _coerce_widget(setting, value)
    ss.validate(setting, coerced)
    if setting.target == "config":
        writer.set_config_value(setting.key, coerced, config_dir=config_dir)
    else:
        writer.set_portfolio_value(setting.key, coerced, config_dir=config_dir)
    return coerced


def apply_provider(
    name: str,
    enabled: bool,
    priority: object,
    api_key_env: str | None,
    key_value: str,
    config_dir: Path | None = None,
) -> None:
    """Persist a provider's enabled/priority and (if provided) its API key."""
    writer.set_provider_field(name, "enabled", bool(enabled), config_dir=config_dir)
    writer.set_provider_field(name, "priority", int(priority), config_dir=config_dir)  # type: ignore[arg-type]
    if api_key_env and key_value:
        writer.set_credential(api_key_env, key_value, config_dir=config_dir)


# ---------------------------------------------------------------------------
# NiceGUI rendering
# ---------------------------------------------------------------------------


def render_settings_section(base_dir: Path) -> None:
    """Render the editable Settings section into the current NiceGUI context."""
    from nicegui import ui

    @ui.refreshable
    def settings_form() -> None:
        cfg = load_config(force_reload=True)

        # -- scalar setting groups --
        for group in ss.groups():
            with ui.card().classes("w-full"):
                ui.label(group).classes("font-bold")
                widgets: dict[str, object] = {}
                for setting in ss.APP_SETTINGS:
                    if setting.group != group:
                        continue
                    widgets[setting.key] = _scalar_widget(ui, setting, ss.get_current(cfg, setting))

                def save_group(bound_widgets: dict = widgets) -> None:
                    saved = 0
                    for key, widget in bound_widgets.items():
                        setting = ss.find(key)
                        if setting is None:
                            continue
                        try:
                            apply_scalar(setting, widget.value, config_dir=base_dir)  # type: ignore[attr-defined]
                            saved += 1
                        except ValueError as exc:
                            ui.notify(f"{key}: {exc}", type="negative")
                            return
                    ui.notify(f"Saved {saved} setting(s)", type="positive")
                    settings_form.refresh()

                ui.button("Save", on_click=save_group).props("color=primary")

        # -- providers --
        with ui.card().classes("w-full"):
            ui.label("Providers").classes("font-bold")
            ui.label("Enable a provider and set its API key (write-only).").classes(
                "text-gray-500 text-sm"
            )
            for row in ss.provider_settings(cfg):
                _provider_card(ui, row, base_dir, settings_form.refresh)

    settings_form()


def _scalar_widget(ui: object, setting: ss.Setting, current: object) -> object:
    """Build the input widget for a scalar setting, seeded with its current value."""
    label = setting.label
    if setting.kind == "bool":
        return ui.switch(label, value=bool(current))  # type: ignore[attr-defined]
    if setting.kind == "choice":
        return ui.select(list(setting.choices or ()), value=current, label=label).classes(  # type: ignore[attr-defined]
            "w-64"
        )
    if setting.kind in ("int", "float"):
        return ui.number(label, value=current).classes("w-64")  # type: ignore[attr-defined]
    # str / cron
    text = "" if current is None else str(current)
    widget = ui.input(label, value=text).classes("w-full")  # type: ignore[attr-defined]
    if setting.help:
        widget.tooltip(setting.help)
    return widget


def _provider_card(ui: object, row: ss.ProviderRow, base_dir: Path, on_saved: object) -> None:
    """Render one provider row: enabled switch, priority, masked key input, Save."""
    with ui.card().classes("w-full"):  # type: ignore[attr-defined]
        with ui.row().classes("items-center gap-4 w-full"):  # type: ignore[attr-defined]
            ui.label(f"{row.name} ({row.kind})").classes("font-bold")  # type: ignore[attr-defined]
            enabled = ui.switch("Enabled", value=row.enabled)  # type: ignore[attr-defined]
            priority = ui.number("Priority", value=row.priority).classes("w-32")  # type: ignore[attr-defined]
        key_input = None
        if row.api_key_env:
            placeholder = "•••• set — leave blank to keep" if row.has_key else "not set"
            key_input = ui.input(  # type: ignore[attr-defined]
                row.api_key_env, password=True, placeholder=placeholder
            ).classes("w-full")

        def save(
            name: str = row.name,
            env: str | None = row.api_key_env,
            en: object = enabled,
            prio: object = priority,
            key_widget: object = key_input,
        ) -> None:
            key_value = (key_widget.value or "").strip() if key_widget is not None else ""  # type: ignore[attr-defined]
            try:
                apply_provider(name, en.value, prio.value, env, key_value, config_dir=base_dir)  # type: ignore[attr-defined]
            except ValueError as exc:
                ui.notify(str(exc), type="negative")  # type: ignore[attr-defined]
                return
            ui.notify(f"Saved {name}", type="positive")  # type: ignore[attr-defined]
            on_saved()  # type: ignore[operator]

        ui.button("Save", on_click=save).props("color=primary flat")  # type: ignore[attr-defined]


__all__ = ["apply_provider", "apply_scalar", "render_settings_section"]
