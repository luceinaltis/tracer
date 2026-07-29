"""Provider lifecycle — optional ``initialize`` / ``health_check`` / ``shutdown`` hooks.

Adapters registered via :mod:`qracer.provider_catalog` (built-ins and
entry-point plugins) can optionally implement the :class:`LifecycleProvider`
protocol to get:

* ``initialize()`` — one-shot async setup (open HTTP sessions, prime caches)
* ``health_check() -> bool`` — runtime readiness probe; ``False`` excludes the
  provider from the registry instead of letting it crash on first use
* ``shutdown()`` — graceful teardown invoked on server exit

All three hooks are optional.  Adapters that don't implement them are treated
as always-healthy and require no teardown — this keeps the system fully
backwards-compatible with the existing adapters in ``qracer/data/`` and
``qracer/llm/``.

Hook failures never propagate: a raising ``initialize``/``health_check`` is
treated as "unhealthy" (provider is skipped with a warning), and a raising
``shutdown`` is logged but otherwise ignored so one bad provider can't block
the rest of the teardown sweep.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Iterable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class LifecycleProvider(Protocol):
    """Optional lifecycle hooks for data and LLM providers.

    Providers are expected to implement **any subset** of these methods — the
    helpers in this module use ``hasattr``/``callable`` duck-typing rather
    than strict protocol conformance, so partial implementations are fine.
    """

    async def initialize(self) -> None: ...

    async def health_check(self) -> bool: ...

    async def shutdown(self) -> None: ...


async def _maybe_await(value: Any) -> Any:
    """Await *value* if it's a coroutine/awaitable, otherwise return it."""
    if inspect.isawaitable(value):
        return await value
    return value


async def initialize_provider(name: str, adapter: Any) -> bool:
    """Run the optional ``initialize()`` then ``health_check()`` hooks.

    Returns ``True`` if the adapter is healthy (or has no hooks at all).
    Returns ``False`` only when a hook raises or ``health_check()`` reports
    the adapter is not ready; either case is logged as a warning.
    """
    init = getattr(adapter, "initialize", None)
    if callable(init):
        try:
            await _maybe_await(init())
        except Exception as exc:
            logger.warning("Provider '%s' initialize() failed: %s", name, exc)
            return False

    health = getattr(adapter, "health_check", None)
    if callable(health):
        try:
            ok = await _maybe_await(health())
        except Exception as exc:
            logger.warning("Provider '%s' health_check() raised: %s", name, exc)
            return False
        if not bool(ok):
            logger.warning("Provider '%s' reported unhealthy — excluded", name)
            return False

    return True


async def shutdown_provider(name: str, adapter: Any) -> None:
    """Run the optional ``shutdown()`` hook, swallowing any exception."""
    shutdown = getattr(adapter, "shutdown", None)
    if not callable(shutdown):
        return
    try:
        await _maybe_await(shutdown())
    except Exception as exc:
        logger.warning("Provider '%s' shutdown() raised: %s", name, exc)


def initialize_provider_sync(name: str, adapter: Any) -> bool:
    """Synchronous wrapper for :func:`initialize_provider`.

    Used by :func:`qracer.cli._build_registries`, which is called from
    synchronous CLI setup code (no running event loop).  If a loop is
    already running we fall through to ``True`` without invoking hooks,
    since ``asyncio.run()`` would fail — in practice the registry build
    path is always sync.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(initialize_provider(name, adapter))
    # Running loop detected — can't safely invoke. Skip lifecycle gracefully.
    logger.debug(
        "Skipping lifecycle init for '%s': running event loop present in sync context",
        name,
    )
    return True


def _iter_unique_adapters(
    registries: Iterable[Any],
) -> list[tuple[str, Any]]:
    """Collect unique ``(name, adapter)`` pairs from one or more registries.

    Accepts both :class:`~qracer.data.registry.DataRegistry` and
    :class:`~qracer.llm.registry.LLMRegistry` — anything exposing the private
    ``_adapters`` or ``_providers`` dicts used by ``register()`` is fair game.
    An adapter registered for multiple capabilities/roles is returned once.
    """
    seen: set[int] = set()
    out: list[tuple[str, Any]] = []
    for reg in registries:
        buckets: dict[Any, list[tuple[str, Any]]] | None = getattr(reg, "_adapters", None)
        if buckets is None:
            buckets = getattr(reg, "_providers", None)
        if not buckets:
            continue
        for entries in buckets.values():
            for name, adapter in entries:
                key = id(adapter)
                if key in seen:
                    continue
                seen.add(key)
                out.append((name, adapter))
    return out


async def shutdown_all_providers(*registries: Any) -> None:
    """Shut down every unique adapter across the given registries.

    Safe to call even when no adapters implement :class:`LifecycleProvider` —
    adapters without a ``shutdown()`` method are silently skipped.
    """
    for name, adapter in _iter_unique_adapters(registries):
        await shutdown_provider(name, adapter)


def shutdown_all_providers_sync(*registries: Any) -> None:
    """Synchronous wrapper for :func:`shutdown_all_providers`.

    No-op when a loop is already running — callers in async contexts should
    await :func:`shutdown_all_providers` directly.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(shutdown_all_providers(*registries))
        return
    logger.debug("Skipping sync provider shutdown: running event loop present")
