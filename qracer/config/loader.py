"""Config loader with directory resolution and per-file merging.

Resolution order (first found wins):
    1. QRACER_CONFIG_DIR env var
    2. ./.qracer/   (project-local)
    3. ~/.qracer/   (user default)

Merge strategy:
    - Project-local values override user-default values per file.
    - credentials.env is loaded from QRACER_CONFIG_DIR if set, else ~/.qracer/ —
      the same location the config writer writes to, so a saved key is read back.
      It is deliberately NOT read from the project-local ./.qracer/ (avoids picking
      up a secrets file committed to a repo).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[no-redef]
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
from typing import Any

from dotenv import dotenv_values

from qracer.config.models import (
    AppConfig,
    PortfolioConfig,
    ProvidersConfig,
    QracerConfig,
)
from qracer.errors import QracerError

_CONFIG_DIR_NAME = ".qracer"
_CREDENTIALS_FILE = "credentials.env"

# Lazy singleton
_cached_config: QracerConfig | None = None
_config_mtimes: dict[str, float] = {}

_WATCHED_FILES = ("config.toml", "providers.toml", "portfolio.toml", "credentials.env")


def _snapshot_mtimes() -> dict[str, float]:
    """Return current mtime for each config file that exists on disk."""
    snapshot: dict[str, float] = {}
    for d in resolve_config_dirs():
        for fname in _WATCHED_FILES:
            path = d / fname
            if path.is_file():
                snapshot[str(path)] = path.stat().st_mtime
    return snapshot


def has_config_changed() -> bool:
    """Return True if any config file was modified since the last load."""
    current = _snapshot_mtimes()
    return current != _config_mtimes


def _user_dir() -> Path:
    """Return ~/.qracer/."""
    return Path.home() / _CONFIG_DIR_NAME


def _project_dir() -> Path:
    """Return ./.qracer/ relative to cwd."""
    return Path.cwd() / _CONFIG_DIR_NAME


def resolve_config_dirs() -> list[Path]:
    """Return config directories in priority order (highest first).

    Only directories that actually exist on disk are returned.
    """
    candidates: list[Path] = []

    env_dir = os.environ.get("QRACER_CONFIG_DIR")
    if env_dir:
        candidates.append(Path(env_dir))

    candidates.append(_project_dir())
    candidates.append(_user_dir())

    return [d for d in candidates if d.is_dir()]


class ConfigParseError(QracerError):
    """Raised when a TOML configuration file exists but cannot be parsed."""


def _load_toml(path: Path) -> dict[str, Any]:
    """Read a TOML file, returning an empty dict if missing.

    Raises :class:`ConfigParseError` when the file exists but contains
    invalid TOML so that callers (and users) get clear feedback instead
    of silently receiving an empty dict.
    """
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        raise ConfigParseError(f"Failed to parse {path}: {exc}") from exc


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge *override* into *base* (override wins on conflicts)."""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_merged_toml(filename: str, dirs: list[Path]) -> dict[str, Any]:
    """Load *filename* from each dir and merge (later dirs are base, earlier override)."""
    # dirs are in priority order (highest first).  We merge from lowest to highest
    # so that higher-priority values override lower ones.
    result: dict[str, Any] = {}
    for d in reversed(dirs):
        data = _load_toml(d / filename)
        result = _merge_dicts(result, data)
    return result


def _credentials_dir() -> Path:
    """Directory credentials.env is read from: QRACER_CONFIG_DIR if set, else ~/.qracer/.

    Mirrors the config writer's resolution so a key written via ``qracer config`` or
    the web Settings tab is read back. Project-local ``./.qracer/`` is intentionally
    excluded so a repo-committed secrets file is never picked up implicitly.
    """
    env_dir = os.environ.get("QRACER_CONFIG_DIR")
    return Path(env_dir) if env_dir else _user_dir()


def _load_credentials() -> dict[str, str]:
    """Load credentials.env from the resolved credentials directory."""
    creds_path = _credentials_dir() / _CREDENTIALS_FILE
    if not creds_path.is_file():
        return {}
    values = dotenv_values(creds_path)
    return {k: v for k, v in values.items() if v is not None}


def load_config(*, force_reload: bool = False) -> QracerConfig:
    """Load and return the merged QracerConfig (lazy-cached singleton).

    The config is automatically reloaded when any watched file's mtime
    changes on disk (hot-plug).  Pass *force_reload=True* to bypass the
    cache unconditionally (useful in tests).
    """
    global _cached_config, _config_mtimes  # noqa: PLW0603

    if _cached_config is not None and not force_reload and not has_config_changed():
        return _cached_config

    dirs = resolve_config_dirs()

    app_data = _load_merged_toml("config.toml", dirs)
    providers_data = _load_merged_toml("providers.toml", dirs)
    portfolio_data = _load_merged_toml("portfolio.toml", dirs)
    credentials = _load_credentials()

    config = QracerConfig(
        app=AppConfig(**app_data),
        providers=ProvidersConfig(**providers_data) if providers_data else ProvidersConfig(),
        portfolio=PortfolioConfig(**portfolio_data),
        credentials=credentials,
    )

    _config_mtimes = _snapshot_mtimes()
    _cached_config = config
    return config
