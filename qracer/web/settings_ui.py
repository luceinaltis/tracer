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

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from qracer.config import settings_schema as ss
from qracer.config import writer
from qracer.config.loader import load_config

# ---------------------------------------------------------------------------
# Pure apply helpers (no NiceGUI) — unit-tested directly
# ---------------------------------------------------------------------------


def _coerce_widget(setting: ss.Setting, value: object) -> object:
    """Coerce a widget value to the setting's type. Raises ValueError.

    A blank numeric field arrives as ``None``; surface that as a clean ValueError
    rather than letting ``int(None)`` raise TypeError past the caller's handler.
    """
    kind = setting.kind
    if kind in ("int", "float"):
        if value is None or value == "":
            raise ValueError(f"{setting.label} must be a number")
        try:
            return int(value) if kind == "int" else float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{setting.label} must be a number") from exc
    if kind == "bool":
        return bool(value)
    return "" if value is None else str(value)


def _coerce_priority(value: object) -> int:
    if value is None or value == "":
        raise ValueError("Priority must be a number")
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("Priority must be a number") from exc


def apply_scalar(setting: ss.Setting, value: object, config_dir: Path | None = None) -> object:
    """Validate + persist one scalar setting; returns the stored value. Raises ValueError."""
    coerced = _coerce_widget(setting, value)
    ss.validate(setting, coerced)
    if setting.target == "config":
        writer.set_config_value(setting.key, coerced, config_dir=config_dir)
    else:
        writer.set_portfolio_value(setting.key, coerced, config_dir=config_dir)
    return coerced


def apply_group(items: list[tuple[ss.Setting, object]], config_dir: Path | None = None) -> None:
    """Validate ALL settings in a group, then persist them — atomic on failure.

    If any value fails coercion/validation, ValueError is raised before any write,
    so a mid-group error never leaves a partial save on disk.
    """
    coerced: list[tuple[ss.Setting, object]] = []
    for setting, value in items:
        c = _coerce_widget(setting, value)
        ss.validate(setting, c)
        coerced.append((setting, c))
    for setting, c in coerced:
        if setting.target == "config":
            writer.set_config_value(setting.key, c, config_dir=config_dir)
        else:
            writer.set_portfolio_value(setting.key, c, config_dir=config_dir)


def apply_provider(
    name: str,
    enabled: bool,
    priority: object,
    api_key_env: str | None,
    key_value: str,
    config_dir: Path | None = None,
) -> None:
    """Persist a provider's enabled/priority and (if provided) its API key. Raises ValueError."""
    prio = _coerce_priority(priority)  # validate before any write
    writer.set_provider_field(name, "enabled", bool(enabled), config_dir=config_dir)
    writer.set_provider_field(name, "priority", prio, config_dir=config_dir)
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
                widgets: dict[str, Any] = {}
                for setting in ss.APP_SETTINGS:
                    if setting.group != group:
                        continue
                    widgets[setting.key] = _scalar_widget(setting, ss.get_current(cfg, setting))

                def save_group(bound_widgets: dict = widgets) -> None:
                    items = [
                        (setting, widget.value)
                        for key, widget in bound_widgets.items()
                        if (setting := ss.find(key)) is not None
                    ]
                    try:
                        apply_group(items, config_dir=base_dir)  # validates all before writing
                    except ValueError as exc:
                        ui.notify(str(exc), type="negative")
                        return
                    ui.notify(f"Saved {len(items)} setting(s)", type="positive")
                    settings_form.refresh()

                ui.button("Save", on_click=save_group).props("color=primary")

        # -- providers --
        with ui.card().classes("w-full"):
            ui.label("Providers").classes("font-bold")
            ui.label("Enable a provider and set its API key (write-only).").classes(
                "text-gray-500 text-sm"
            )
            for row in ss.provider_settings(cfg):
                _provider_card(row, base_dir, settings_form.refresh)

    settings_form()


def _scalar_widget(setting: ss.Setting, current: object) -> Any:
    """Build the input widget for a scalar setting, seeded with its current value."""
    from nicegui import ui

    label = setting.label
    if setting.kind == "bool":
        return ui.switch(label, value=bool(current))
    if setting.kind == "choice":
        return ui.select(list(setting.choices or ()), value=current, label=label).classes("w-64")
    num = cast("float | None", current)
    if setting.kind == "int":
        return ui.number(label, value=num, format="%.0f", step=1).classes("w-64")
    if setting.kind == "float":
        return ui.number(label, value=num).classes("w-64")
    # str / cron
    text = "" if current is None else str(current)
    widget = ui.input(label, value=text).classes("w-full")
    if setting.help:
        widget.tooltip(setting.help)
    return widget


def _provider_card(row: ss.ProviderRow, base_dir: Path, on_saved: Callable[[], object]) -> None:
    """Render one provider row: enabled switch, priority, masked key input, Save."""
    from nicegui import ui

    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-4 w-full"):
            ui.label(f"{row.name} ({row.kind})").classes("font-bold")
            enabled = ui.switch("Enabled", value=row.enabled)
            priority = ui.number("Priority", value=row.priority).classes("w-32")
        key_input = None
        if row.api_key_env:
            placeholder = "•••• set — leave blank to keep" if row.has_key else "not set"
            key_input = ui.input(row.api_key_env, password=True, placeholder=placeholder).classes(
                "w-full"
            )

        def save(
            name: str = row.name,
            env: str | None = row.api_key_env,
            en: Any = enabled,
            prio: Any = priority,
            key_widget: Any = key_input,
        ) -> None:
            key_value = (key_widget.value or "").strip() if key_widget is not None else ""
            try:
                apply_provider(name, en.value, prio.value, env, key_value, config_dir=base_dir)
            except ValueError as exc:
                ui.notify(str(exc), type="negative")
                return
            ui.notify(f"Saved {name}", type="positive")
            on_saved()

        ui.button("Save", on_click=save).props("color=primary flat")


__all__ = ["apply_group", "apply_provider", "apply_scalar", "render_settings_section"]
