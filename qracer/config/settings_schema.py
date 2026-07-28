"""Declarative settings registry — the single source of truth for editable config.

Each user-facing setting is described once as a :class:`Setting`. Both front-ends
render from this list: the CLI (`qracer config`) turns them into prompts/validation,
and the web Settings tab turns them into form widgets. Adding a setting = one entry
here, and it appears in both places.

Providers straddle two files (``providers.toml`` for enable/priority and
``credentials.env`` for the API key), so they get their own :class:`ProviderRow`
derived from the provider catalog + current config via :func:`provider_settings`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qracer.config.models import QracerConfig

_SCHEMA_DIR = Path(__file__).parent / "schema"


# ---------------------------------------------------------------------------
# Scalar settings (config.toml / portfolio.toml)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Setting:
    """One editable scalar setting.

    ``key`` is the dotted path within its file (which mirrors the attribute path
    under ``cfg.app`` for ``config`` targets and ``cfg.portfolio`` for ``portfolio``).
    """

    key: str
    target: str  # "config" | "portfolio"
    label: str
    group: str
    kind: str  # "bool" | "int" | "float" | "str" | "choice" | "cron"
    choices: tuple[str, ...] | None = None
    help: str = ""
    validator: Callable[[object], str | None] | None = None


def _cron_validator(value: object) -> str | None:
    from qracer.agents_store import validate_cron

    return None if validate_cron(str(value)) else f"Invalid cron expression: {value!r}"


APP_SETTINGS: tuple[Setting, ...] = (
    # -- General --
    Setting(
        "default_mode",
        "config",
        "Default mode",
        "General",
        "choice",
        choices=("quick", "deep"),
        help="Analysis depth for a bare query.",
    ),
    Setting(
        "llm_provider",
        "config",
        "LLM provider",
        "General",
        "str",
        help="Active LLM provider name (must be enabled in Providers).",
    ),
    Setting("llm_model", "config", "LLM model", "General", "str"),
    Setting(
        "language",
        "config",
        "Language",
        "General",
        "str",
        help="Response language code (e.g. en, ko).",
    ),
    # -- Analysis --
    Setting("max_iterations", "config", "Max iterations", "Analysis", "int"),
    Setting(
        "confidence_threshold",
        "config",
        "Confidence threshold",
        "Analysis",
        "float",
        help="0.0–1.0; stop early once confidence exceeds this.",
    ),
    Setting("lookback_days", "config", "Lookback days", "Analysis", "int"),
    Setting("staleness_hours", "config", "Staleness hours", "Analysis", "int"),
    # -- Autonomous monitoring --
    Setting("autonomous_enabled", "config", "Autonomous monitoring", "Autonomous", "bool"),
    Setting("price_move_threshold_pct", "config", "Price-move alert %", "Autonomous", "float"),
    Setting("alert_cooldown_minutes", "config", "Alert cooldown (min)", "Autonomous", "int"),
    # -- Daily briefing --
    Setting("briefing.enabled", "config", "Briefing enabled", "Briefing", "bool"),
    Setting(
        "briefing.schedule",
        "config",
        "Briefing schedule",
        "Briefing",
        "cron",
        help="cron expression (e.g. '0 8 * * 1-5').",
        validator=_cron_validator,
    ),
    Setting(
        "briefing.timezone",
        "config",
        "Briefing timezone",
        "Briefing",
        "str",
        help="IANA tz (e.g. America/New_York); blank = local.",
    ),
    Setting("briefing.top_n", "config", "Briefing items", "Briefing", "int"),
    # -- Portfolio --
    Setting("currency", "portfolio", "Currency", "Portfolio", "str"),
    Setting(
        "limits.max_single_position_pct", "portfolio", "Max single position %", "Portfolio", "float"
    ),
    Setting("limits.max_sector_pct", "portfolio", "Max sector %", "Portfolio", "float"),
    Setting(
        "limits.max_drawdown_alert_pct", "portfolio", "Max drawdown alert %", "Portfolio", "float"
    ),
)

_BY_KEY: dict[str, Setting] = {s.key: s for s in APP_SETTINGS}


def find(key: str) -> Setting | None:
    """Look up a setting by its dotted key (None if unknown)."""
    return _BY_KEY.get(key)


def require(key: str) -> Setting:
    """Look up a setting by key, raising ``KeyError`` if it does not exist."""
    setting = _BY_KEY.get(key)
    if setting is None:
        raise KeyError(f"Unknown setting {key!r}")
    return setting


def groups() -> list[str]:
    """Ordered, de-duplicated list of setting groups."""
    seen: list[str] = []
    for s in APP_SETTINGS:
        if s.group not in seen:
            seen.append(s.group)
    return seen


def get_current(cfg: "QracerConfig", setting: Setting) -> object:
    """Read a setting's current value from a loaded config.

    The dotted key mirrors the attribute path under ``cfg.app`` (config target)
    or ``cfg.portfolio`` (portfolio target).
    """
    node: object = cfg.app if setting.target == "config" else cfg.portfolio
    for part in setting.key.split("."):
        node = getattr(node, part)
    return node


def coerce(setting: Setting, raw: str) -> object:
    """Coerce a raw string (CLI input) to the setting's type. Raises ValueError."""
    kind = setting.kind
    if kind == "bool":
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"Expected a boolean (true/false), got {raw!r}")
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    # str / choice / cron
    return raw


def validate(setting: Setting, value: object) -> None:
    """Validate a coerced value against choices + a custom validator. Raises ValueError."""
    if setting.choices is not None and value not in setting.choices:
        raise ValueError(f"{setting.key} must be one of {', '.join(setting.choices)}")
    if setting.validator is not None:
        err = setting.validator(value)
        if err:
            raise ValueError(err)


# ---------------------------------------------------------------------------
# Providers (providers.toml + credentials.env)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderRow:
    """Current state of one provider, for the Providers settings group."""

    name: str
    kind: str  # "data" | "llm"
    enabled: bool
    priority: int
    api_key_env: str | None
    has_key: bool


def _schema_providers() -> dict[str, dict]:
    from qracer.config.loader import _load_toml

    return _load_toml(_SCHEMA_DIR / "providers.toml").get("providers", {})


def provider_settings(
    cfg: "QracerConfig", credentials: dict[str, str] | None = None
) -> list[ProviderRow]:
    """Build provider rows from the catalog (schema + entry points) overlaid with config.

    Enabled/priority/api_key_env come from the user's config when present, else the
    packaged schema defaults. ``has_key`` reflects credentials.env or the process env.
    """
    from qracer.provider_catalog import discover_data_providers, discover_llm_providers

    creds = credentials if credentials is not None else dict(cfg.credentials)
    schema = _schema_providers()

    # Known provider name -> kind, from the runtime catalog (built-ins + plugins).
    kinds: dict[str, str] = {}
    for name in discover_data_providers():
        kinds[name] = "data"
    for name in discover_llm_providers():
        kinds[name] = "llm"
    # Include any schema-declared providers even if the adapter import is unavailable.
    for name, meta in schema.items():
        kinds.setdefault(name, meta.get("kind", "data"))

    rows: list[ProviderRow] = []
    for name in sorted(kinds):
        current = cfg.providers.providers.get(name)
        sch = schema.get(name, {})
        api_key_env = (current.api_key_env if current else None) or sch.get("api_key_env")
        enabled = current.enabled if current else bool(sch.get("enabled", False))
        priority = current.priority if current else int(sch.get("priority", 100))
        has_key = bool(api_key_env and (creds.get(api_key_env) or os.environ.get(api_key_env)))
        rows.append(
            ProviderRow(
                name=name,
                kind=kinds[name],
                enabled=enabled,
                priority=priority,
                api_key_env=api_key_env,
                has_key=has_key,
            )
        )
    return rows


__all__ = [
    "APP_SETTINGS",
    "ProviderRow",
    "Setting",
    "coerce",
    "find",
    "get_current",
    "groups",
    "provider_settings",
    "require",
    "validate",
]
