"""Tests for qracer.provider_lifecycle."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from qracer.provider_lifecycle import (
    LifecycleProvider,
    initialize_provider,
    initialize_provider_sync,
    shutdown_all_providers,
    shutdown_all_providers_sync,
    shutdown_provider,
)

# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class _AsyncAdapter:
    """Adapter that implements every hook as an async coroutine."""

    def __init__(self, *, healthy: bool = True, fail_init: bool = False) -> None:
        self.init_calls = 0
        self.health_calls = 0
        self.shutdown_calls = 0
        self._healthy = healthy
        self._fail_init = fail_init

    async def initialize(self) -> None:
        self.init_calls += 1
        if self._fail_init:
            raise RuntimeError("init boom")

    async def health_check(self) -> bool:
        self.health_calls += 1
        return self._healthy

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class _SyncAdapter:
    """Adapter that implements hooks as plain (non-async) methods."""

    def __init__(self, *, healthy: bool = True) -> None:
        self.init_calls = 0
        self.shutdown_calls = 0
        self._healthy = healthy

    def initialize(self) -> None:
        self.init_calls += 1

    def health_check(self) -> bool:
        return self._healthy

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class _NoHookAdapter:
    """Adapter with no lifecycle methods — the common built-in case."""


class _RaisingShutdownAdapter:
    async def shutdown(self) -> None:
        raise RuntimeError("shutdown boom")


class _RaisingHealthAdapter:
    async def health_check(self) -> bool:
        raise RuntimeError("health boom")


class _FakeRegistry:
    """Minimal stand-in that mimics DataRegistry / LLMRegistry storage."""

    def __init__(self, adapters_attr: str = "_adapters") -> None:
        setattr(self, adapters_attr, {})
        self._key = adapters_attr

    def register(self, capability: Any, name: str, adapter: Any) -> None:
        bucket = getattr(self, self._key).setdefault(capability, [])
        bucket.append((name, adapter))


# --------------------------------------------------------------------------
# Protocol detection
# --------------------------------------------------------------------------


class TestLifecycleProtocol:
    def test_async_adapter_matches_protocol(self) -> None:
        assert isinstance(_AsyncAdapter(), LifecycleProvider)

    def test_no_hook_adapter_does_not_match(self) -> None:
        assert not isinstance(_NoHookAdapter(), LifecycleProvider)


# --------------------------------------------------------------------------
# initialize_provider
# --------------------------------------------------------------------------


class TestInitializeProvider:
    @pytest.mark.asyncio
    async def test_async_hooks_called_and_healthy(self) -> None:
        a = _AsyncAdapter()
        assert await initialize_provider("acme", a) is True
        assert a.init_calls == 1
        assert a.health_calls == 1

    @pytest.mark.asyncio
    async def test_sync_hooks_supported(self) -> None:
        a = _SyncAdapter()
        assert await initialize_provider("acme", a) is True
        assert a.init_calls == 1

    @pytest.mark.asyncio
    async def test_no_hooks_always_healthy(self) -> None:
        assert await initialize_provider("acme", _NoHookAdapter()) is True

    @pytest.mark.asyncio
    async def test_unhealthy_returns_false(self, caplog: pytest.LogCaptureFixture) -> None:
        a = _AsyncAdapter(healthy=False)
        caplog.set_level(logging.WARNING, logger="qracer.provider_lifecycle")
        assert await initialize_provider("acme", a) is False
        assert any("reported unhealthy" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_initialize_failure_returns_false(self, caplog: pytest.LogCaptureFixture) -> None:
        a = _AsyncAdapter(fail_init=True)
        caplog.set_level(logging.WARNING, logger="qracer.provider_lifecycle")
        assert await initialize_provider("acme", a) is False
        # health_check should not be called once init fails
        assert a.health_calls == 0
        assert any("initialize() failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_health_check_exception_returns_false(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="qracer.provider_lifecycle")
        assert await initialize_provider("acme", _RaisingHealthAdapter()) is False
        assert any("health_check() raised" in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# shutdown_provider
# --------------------------------------------------------------------------


class TestShutdownProvider:
    @pytest.mark.asyncio
    async def test_shutdown_invoked(self) -> None:
        a = _AsyncAdapter()
        await shutdown_provider("acme", a)
        assert a.shutdown_calls == 1

    @pytest.mark.asyncio
    async def test_sync_shutdown_invoked(self) -> None:
        a = _SyncAdapter()
        await shutdown_provider("acme", a)
        assert a.shutdown_calls == 1

    @pytest.mark.asyncio
    async def test_no_hook_is_noop(self) -> None:
        # Should not raise.
        await shutdown_provider("acme", _NoHookAdapter())

    @pytest.mark.asyncio
    async def test_exception_is_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger="qracer.provider_lifecycle")
        # Should not raise.
        await shutdown_provider("acme", _RaisingShutdownAdapter())
        assert any("shutdown() raised" in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# Sync wrappers
# --------------------------------------------------------------------------


class TestSyncWrappers:
    def test_initialize_sync_runs_async_hooks(self) -> None:
        a = _AsyncAdapter()
        assert initialize_provider_sync("acme", a) is True
        assert a.init_calls == 1
        assert a.health_calls == 1

    def test_initialize_sync_unhealthy(self) -> None:
        a = _AsyncAdapter(healthy=False)
        assert initialize_provider_sync("acme", a) is False

    def test_initialize_sync_no_hooks(self) -> None:
        assert initialize_provider_sync("acme", _NoHookAdapter()) is True

    def test_initialize_sync_with_running_loop_is_noop(self) -> None:
        """When a loop is running, sync wrapper returns True without invoking hooks."""
        a = _AsyncAdapter()

        async def _runner() -> bool:
            return initialize_provider_sync("acme", a)

        result = asyncio.run(_runner())
        assert result is True
        # Hooks are skipped in the running-loop branch.
        assert a.init_calls == 0

    def test_shutdown_all_sync(self) -> None:
        a = _AsyncAdapter()
        reg = _FakeRegistry()
        reg.register("cap", "acme", a)
        shutdown_all_providers_sync(reg)
        assert a.shutdown_calls == 1

    def test_shutdown_all_sync_with_running_loop_is_noop(self) -> None:
        a = _AsyncAdapter()
        reg = _FakeRegistry()
        reg.register("cap", "acme", a)

        async def _runner() -> None:
            shutdown_all_providers_sync(reg)

        asyncio.run(_runner())
        assert a.shutdown_calls == 0


# --------------------------------------------------------------------------
# shutdown_all_providers
# --------------------------------------------------------------------------


class TestShutdownAll:
    @pytest.mark.asyncio
    async def test_deduplicates_shared_adapter(self) -> None:
        """One adapter registered under two capabilities is shut down once."""
        a = _AsyncAdapter()
        reg = _FakeRegistry()
        reg.register("cap_a", "acme", a)
        reg.register("cap_b", "acme", a)
        await shutdown_all_providers(reg)
        assert a.shutdown_calls == 1

    @pytest.mark.asyncio
    async def test_handles_llm_style_registry(self) -> None:
        """Accepts registries that store adapters under ``_providers``."""
        a = _AsyncAdapter()
        reg = _FakeRegistry(adapters_attr="_providers")
        reg.register("role", "acme", a)
        await shutdown_all_providers(reg)
        assert a.shutdown_calls == 1

    @pytest.mark.asyncio
    async def test_empty_registries_noop(self) -> None:
        # No _adapters/_providers attr at all.
        class _Bare:
            pass

        await shutdown_all_providers(_Bare())  # should not raise

    @pytest.mark.asyncio
    async def test_mixed_registries(self) -> None:
        """Both data- and llm-style registries visited in a single call."""
        a = _AsyncAdapter()
        b = _AsyncAdapter()
        data = _FakeRegistry()
        llm = _FakeRegistry(adapters_attr="_providers")
        data.register("cap", "a-adapter", a)
        llm.register("role", "b-adapter", b)
        await shutdown_all_providers(data, llm)
        assert a.shutdown_calls == 1
        assert b.shutdown_calls == 1

    @pytest.mark.asyncio
    async def test_one_failure_does_not_stop_others(self) -> None:
        good = _AsyncAdapter()
        bad = _RaisingShutdownAdapter()
        reg = _FakeRegistry()
        reg.register("cap", "good", good)
        reg.register("cap", "bad", bad)
        await shutdown_all_providers(reg)
        # Good adapter still shut down despite sibling exception.
        assert good.shutdown_calls == 1
