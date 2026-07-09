"""Custom agents — user-defined workers (model + free-form prompt + trigger).

Each agent is an independent worker: a chosen LLM model plus a free-form prompt
describing what it should do. Agents run either manually (on demand), on a cron
schedule, or continuously, and their latest results are stored back here so the
web UI can display them.

Persisted as ``agents.json`` in the user's ``~/.qracer/`` directory. The store
hot-reloads on mtime change so the ``qracer serve`` daemon, the CLI, and the web
UI can share the same file and see each other's writes.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, cast

from croniter import croniter

logger = logging.getLogger(__name__)

# Continuous agents re-run every tick; this floor keeps them from hammering the
# provider faster than the daemon's own polling cadence would otherwise allow.
CONTINUOUS_COOLDOWN_SECONDS = 60


class TriggerType(str, Enum):
    """How an agent is scheduled."""

    MANUAL = "manual"  # only via `/run` / `qracer run`
    CRON = "cron"  # croniter expression
    CONTINUOUS = "continuous"  # runs every cycle (subject to a cooldown)


def validate_cron(expr: str) -> bool:
    """Return True if *expr* is a valid cron expression."""
    return bool(expr) and croniter.is_valid(expr)


def compute_cron_next(expr: str, after: datetime | None = None) -> str:
    """Return the next run time for a cron expression as an ISO string (UTC)."""
    base = after or datetime.now(timezone.utc)
    return cast(datetime, croniter(expr, base).get_next(datetime)).isoformat()


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class Agent:
    """A user-defined autonomous agent."""

    id: str
    name: str
    model: str
    prompt: str
    trigger_type: TriggerType = TriggerType.MANUAL
    cron: str | None = None
    enabled: bool = True
    created_at: str = ""
    # runtime state (written by the daemon / runner)
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_output: str | None = None
    last_error: str | None = None
    run_count: int = 0

    def is_due(
        self,
        now: datetime | None = None,
        *,
        continuous_cooldown: int = CONTINUOUS_COOLDOWN_SECONDS,
    ) -> bool:
        """Return True if this agent should run at *now*."""
        if not self.enabled:
            return False
        ref = now or datetime.now(timezone.utc)

        if self.trigger_type == TriggerType.CRON:
            if not self.next_run_at:
                return False
            return _parse_iso(self.next_run_at) <= ref

        if self.trigger_type == TriggerType.CONTINUOUS:
            if self.last_run_at is None:
                return True
            elapsed = (ref - _parse_iso(self.last_run_at)).total_seconds()
            return elapsed >= continuous_cooldown

        return False  # MANUAL never auto-runs

    def describe(self) -> str:
        """Return a human-readable description."""
        if self.trigger_type == TriggerType.CRON:
            trigger = f"cron {self.cron}"
        else:
            trigger = self.trigger_type.value
        return f"{self.name} [{self.model}] ({trigger})"


class AgentStore:
    """File-backed storage for custom agents.

    Usage::

        store = AgentStore(Path("~/.qracer/agents.json"))
        agent = store.create("News watcher", "openai/gpt-4o", "Scan tech news...",
                              trigger_type=TriggerType.CRON, cron="*/5 * * * *")
        due = store.due()
        store.record_run(agent.id, output="...")
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._mtime: float = 0.0
        self._agents: list[Agent] = self._load()

    @property
    def agents(self) -> list[Agent]:
        """All agents in stored order."""
        self._maybe_reload()
        return list(self._agents)

    def get(self, agent_id: str) -> Agent | None:
        self._maybe_reload()
        return next((a for a in self._agents if a.id == agent_id), None)

    def find_by_name(self, name: str) -> Agent | None:
        """Case-insensitive lookup by name."""
        self._maybe_reload()
        lowered = name.strip().lower()
        return next((a for a in self._agents if a.name.lower() == lowered), None)

    def create(
        self,
        name: str,
        model: str,
        prompt: str,
        *,
        trigger_type: TriggerType = TriggerType.MANUAL,
        cron: str | None = None,
        enabled: bool = True,
    ) -> Agent:
        """Create and persist a new agent."""
        now = datetime.now(timezone.utc)
        next_run: str | None = None
        if trigger_type == TriggerType.CRON:
            if not validate_cron(cron or ""):
                raise ValueError(f"Invalid cron expression: {cron!r}")
            next_run = compute_cron_next(cast(str, cron), now)
        agent = Agent(
            id=uuid.uuid4().hex[:8],
            name=name,
            model=model,
            prompt=prompt,
            trigger_type=trigger_type,
            cron=cron,
            enabled=enabled,
            created_at=now.isoformat(),
            next_run_at=next_run,
        )
        self._agents.append(agent)
        self._save()
        return agent

    def update(self, agent_id: str, **fields: Any) -> Agent | None:
        """Update mutable fields of an agent. Returns the agent, or None if missing.

        Recomputes ``next_run_at`` whenever the trigger/cron changes.
        """
        agent = next((a for a in self._agents if a.id == agent_id), None)
        if agent is None:
            return None

        allowed = {"name", "model", "prompt", "enabled", "trigger_type", "cron"}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "trigger_type" and not isinstance(value, TriggerType):
                value = TriggerType(value)
            setattr(agent, key, value)

        if agent.trigger_type == TriggerType.CRON:
            if not validate_cron(agent.cron or ""):
                raise ValueError(f"Invalid cron expression: {agent.cron!r}")
            agent.next_run_at = compute_cron_next(cast(str, agent.cron), datetime.now(timezone.utc))
        else:
            agent.next_run_at = None

        self._save()
        return agent

    def remove(self, agent_id: str) -> bool:
        """Remove an agent by id. Returns True if found and removed."""
        for i, a in enumerate(self._agents):
            if a.id == agent_id:
                self._agents.pop(i)
                self._save()
                return True
        return False

    def reorder(self, ids: list[str]) -> None:
        """Reorder agents to match *ids*; any not listed keep their relative order at the end."""
        index = {aid: pos for pos, aid in enumerate(ids)}
        original = {a.id: i for i, a in enumerate(self._agents)}
        self._agents.sort(key=lambda a: index.get(a.id, len(index) + original[a.id]))
        self._save()

    def due(
        self,
        now: datetime | None = None,
        *,
        continuous_cooldown: int = CONTINUOUS_COOLDOWN_SECONDS,
    ) -> list[Agent]:
        """Return agents that should run now (cron due or continuous off cooldown)."""
        self._maybe_reload()
        ref = now or datetime.now(timezone.utc)
        return [
            a for a in self._agents if a.is_due(ref, continuous_cooldown=continuous_cooldown)
        ]

    def record_run(
        self,
        agent_id: str,
        *,
        output: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Persist the result of a run and advance the schedule. Returns True if found."""
        for a in self._agents:
            if a.id != agent_id:
                continue
            now = datetime.now(timezone.utc)
            a.last_run_at = now.isoformat()
            a.run_count += 1
            a.last_output = output
            a.last_error = error
            if a.trigger_type == TriggerType.CRON and a.cron:
                a.next_run_at = compute_cron_next(a.cron, now)
            self._save()
            return True
        return False

    def clear(self) -> None:
        self._agents.clear()
        self._save()

    def __len__(self) -> int:
        return len(self._agents)

    # -- persistence --------------------------------------------------------

    def _maybe_reload(self) -> None:
        """Re-read from disk if another process modified the file."""
        if not self._path.exists():
            return
        try:
            current_mtime = self._path.stat().st_mtime
        except OSError:
            return
        if current_mtime != self._mtime:
            self._agents = self._load()

    def _load(self) -> list[Agent]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._mtime = self._path.stat().st_mtime
            if isinstance(data, list):
                return [self._deserialize(item) for item in data if isinstance(item, dict)]
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            logger.warning("Failed to load agents from %s", self._path)
        return []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = [self._serialize(a) for a in self._agents]
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self._mtime = self._path.stat().st_mtime

    @staticmethod
    def _serialize(agent: Agent) -> dict[str, Any]:
        d = asdict(agent)
        d["trigger_type"] = agent.trigger_type.value
        return d

    @staticmethod
    def _deserialize(data: dict[str, Any]) -> Agent:
        return Agent(
            id=data["id"],
            name=data["name"],
            model=data["model"],
            prompt=data["prompt"],
            trigger_type=TriggerType(data.get("trigger_type", "manual")),
            cron=data.get("cron"),
            enabled=data.get("enabled", True),
            created_at=data.get("created_at", ""),
            next_run_at=data.get("next_run_at"),
            last_run_at=data.get("last_run_at"),
            last_output=data.get("last_output"),
            last_error=data.get("last_error"),
            run_count=data.get("run_count", 0),
        )


__all__ = [
    "Agent",
    "AgentStore",
    "TriggerType",
    "validate_cron",
    "compute_cron_next",
    "CONTINUOUS_COOLDOWN_SECONDS",
]
