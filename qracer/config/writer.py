"""Structured, comment-preserving writer for the .qracer/ config files.

This is the single write path for configuration, used by both the CLI
(`qracer config`, `qracer install`) and the web Settings tab. Unlike the old
regex / flat-dict hacks it uses ``tomlkit`` for round-trip editing, so existing
comments, formatting, and nested tables (``[briefing]``, ``[providers.*]``,
``[limits]``) survive an edit.

Writes go to ``~/.qracer/`` by default (the canonical writable location); a
``config_dir`` override is accepted for tests and for the web layer. When a
target file does not exist yet it is seeded from the packaged schema template
so the helpful default comments are preserved.
"""

from __future__ import annotations

import copy
import os
import shutil
from pathlib import Path
from typing import Any

import tomlkit
from dotenv import dotenv_values
from tomlkit.items import Table

from qracer.config.loader import _user_dir

_SCHEMA_DIR = Path(__file__).parent / "schema"
_CREDENTIALS_FILE = "credentials.env"


# ---------------------------------------------------------------------------
# Low-level file helpers
# ---------------------------------------------------------------------------


def _resolve_dir(config_dir: Path | None) -> Path:
    """Directory to write into.

    An explicit ``config_dir`` wins; otherwise honor ``$QRACER_CONFIG_DIR`` so
    reads and writes target the same directory, falling back to ``~/.qracer/``.
    """
    if config_dir is not None:
        return config_dir
    env_dir = os.environ.get("QRACER_CONFIG_DIR")
    return Path(env_dir) if env_dir else _user_dir()


def _ensure_toml(config_dir: Path | None, filename: str) -> Path:
    """Return the path to *filename*, seeding it from the schema template if absent."""
    base = _resolve_dir(config_dir)
    base.mkdir(parents=True, exist_ok=True)
    path = base / filename
    if not path.exists():
        template = _SCHEMA_DIR / filename
        if template.exists():
            shutil.copy2(template, path)
        else:  # pragma: no cover - all config files have a template
            path.write_text("", encoding="utf-8")
    return path


def _load_doc(path: Path) -> tomlkit.TOMLDocument:
    if path.exists():
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    return tomlkit.document()


def _write_doc(path: Path, doc: tomlkit.TOMLDocument) -> None:
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def _set_dotted(doc: tomlkit.TOMLDocument, dotted_key: str, value: object) -> None:
    """Set ``a.b.c = value``, creating intermediate tables as needed."""
    parts = dotted_key.split(".")
    node: Any = doc
    for part in parts[:-1]:
        child = node.get(part)
        # Create (or replace a stray scalar with) an intermediate table so a
        # malformed file like `briefing = "x"` can't crash the write.
        if not isinstance(child, Table):
            child = tomlkit.table()
            node[part] = child
        node = child
    node[parts[-1]] = value


# ---------------------------------------------------------------------------
# config.toml / portfolio.toml
# ---------------------------------------------------------------------------


def set_config_value(dotted_key: str, value: object, config_dir: Path | None = None) -> Path:
    """Set a value in ``config.toml`` (e.g. ``default_mode`` or ``briefing.schedule``)."""
    path = _ensure_toml(config_dir, "config.toml")
    doc = _load_doc(path)
    _set_dotted(doc, dotted_key, value)
    _write_doc(path, doc)
    return path


def set_portfolio_value(dotted_key: str, value: object, config_dir: Path | None = None) -> Path:
    """Set a value in ``portfolio.toml`` (e.g. ``currency`` or ``limits.max_sector_pct``)."""
    path = _ensure_toml(config_dir, "portfolio.toml")
    doc = _load_doc(path)
    _set_dotted(doc, dotted_key, value)
    _write_doc(path, doc)
    return path


# ---------------------------------------------------------------------------
# providers.toml
# ---------------------------------------------------------------------------


def _schema_provider_block(name: str) -> Table:
    """A fresh table seeded from the schema's ``[providers.<name>]`` (defaults if unknown)."""
    schema = _load_doc(_SCHEMA_DIR / "providers.toml")
    src = schema.get("providers", {}).get(name)
    if src is not None:
        # Deep-copy the schema item so its comments/formatting come along.
        return copy.deepcopy(src)
    table = tomlkit.table()  # unknown provider — minimal sane defaults
    table["enabled"] = False
    table["priority"] = 50
    return table


def set_provider_field(
    name: str, field: str, value: object, config_dir: Path | None = None
) -> Path:
    """Set ``[providers.<name>] <field> = value`` in ``providers.toml``.

    Creates the provider block (seeded from the schema) if the user's file does
    not have it yet.
    """
    path = _ensure_toml(config_dir, "providers.toml")
    doc = _load_doc(path)
    providers = doc.get("providers")
    if providers is None:
        providers = tomlkit.table(is_super_table=True)
        doc["providers"] = providers
    if name not in providers:
        providers[name] = _schema_provider_block(name)
    providers[name][field] = value
    _write_doc(path, doc)
    return path


# ---------------------------------------------------------------------------
# credentials.env (dotenv, not TOML)
# ---------------------------------------------------------------------------


def _credentials_path(config_dir: Path | None) -> Path:
    base = _resolve_dir(config_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base / _CREDENTIALS_FILE


def set_credential(env_key: str, value: str, config_dir: Path | None = None) -> Path:
    """Upsert ``env_key=value`` in ``credentials.env``, preserving other lines/comments."""
    path = _credentials_path(config_dir)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    new_line = f"{env_key}={value}"
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if (
            stripped
            and not stripped.startswith("#")
            and stripped.split("=", 1)[0].strip() == env_key
        ):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def unset_credential(env_key: str, config_dir: Path | None = None) -> Path:
    """Remove ``env_key`` from ``credentials.env`` (no-op if absent)."""
    path = _credentials_path(config_dir)
    if not path.exists():
        return path
    kept = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not (
            line.strip()
            and not line.strip().startswith("#")
            and line.strip().split("=", 1)[0].strip() == env_key
        )
    ]
    path.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    return path


def read_credentials(config_dir: Path | None = None) -> dict[str, str]:
    """Return the ``credentials.env`` key/value pairs (empty if the file is absent).

    Uses the same ``dotenv`` parser as the loader so the values match exactly
    (quote stripping, ``export`` prefixes, inline comments).
    """
    path = _credentials_path(config_dir)
    if not path.exists():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


__all__ = [
    "read_credentials",
    "set_config_value",
    "set_credential",
    "set_portfolio_value",
    "set_provider_field",
    "unset_credential",
]
