"""Cross-session persistence for ``ConversationContext``.

The in-session ``ConversationContext`` is rebuilt from the session log on
every query.  This module adds a durable layer: the context is serialized
to ``~/.qracer/context.json`` at the end of each query so a returning user
keeps their topic stack, last intent, and last activity timestamp across
restarts.

The persisted state is conservative — topics older than
``DEFAULT_STALE_DAYS`` are discarded on load, and open theses from the
``FactStore`` are folded in so a returning user sees both recently
discussed tickers and still-active theses in their topic stack.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from qracer.conversation.constants import MAX_TOPIC_STACK
from qracer.conversation.context import ConversationContext

logger = logging.getLogger(__name__)

DEFAULT_STALE_DAYS = 7


def save_context(context: ConversationContext, path: Path) -> None:
    """Serialize a ``ConversationContext`` to ``path`` as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "current_topic": context.current_topic,
        "topic_stack": list(context.topic_stack),
        "intent": context.intent,
        "depth": context.depth,
        "last_activity": context.last_activity.isoformat(),
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_context(path: Path) -> ConversationContext:
    """Load a persisted ``ConversationContext`` from ``path``.

    Returns an empty context if the file is missing or malformed so the
    caller never has to special-case a first-run user.
    """
    if not path.exists():
        return ConversationContext()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to load persisted context from %s", path, exc_info=True)
        return ConversationContext()
    if not isinstance(data, dict):
        return ConversationContext()
    try:
        last_activity = datetime.fromisoformat(str(data.get("last_activity", "")))
    except (ValueError, TypeError):
        last_activity = datetime.now()
    topic_stack_raw = data.get("topic_stack") or []
    topic_stack = [t for t in topic_stack_raw if isinstance(t, str)]
    current_topic = data.get("current_topic")
    intent = data.get("intent")
    depth = data.get("depth")
    return ConversationContext(
        current_topic=current_topic if isinstance(current_topic, str) else None,
        topic_stack=topic_stack,
        intent=intent if isinstance(intent, str) else None,
        depth=depth if isinstance(depth, str) else "quick",
        last_activity=last_activity,
    )


def decay_stale(
    context: ConversationContext, max_age_days: int = DEFAULT_STALE_DAYS
) -> ConversationContext:
    """Return a context with its topic stack cleared if it is stale.

    The data model tracks a single ``last_activity`` for the whole
    conversation rather than per-topic timestamps, so "stale topics" is
    implemented as "the whole stack is abandoned once the gap exceeds
    the threshold".  The returned context keeps ``last_activity`` so
    downstream code can still detect staleness for messaging.
    """
    if not context.topic_stack and context.current_topic is None:
        return context
    age = datetime.now() - context.last_activity
    if age > timedelta(days=max_age_days):
        return ConversationContext(last_activity=context.last_activity)
    return context


def merge_with_theses(
    context: ConversationContext, thesis_tickers: list[str]
) -> ConversationContext:
    """Fold still-open ``FactStore`` theses into the topic stack.

    Existing entries keep their order; new ticker candidates are appended
    up to ``MAX_TOPIC_STACK``.  ``current_topic`` is seeded from the
    topic stack if unset so a returning user with no session history
    still gets a meaningful current topic.
    """
    topic_stack = list(context.topic_stack)
    for ticker in thesis_tickers:
        if ticker not in topic_stack and len(topic_stack) < MAX_TOPIC_STACK:
            topic_stack.append(ticker)
    return ConversationContext(
        current_topic=context.current_topic or (topic_stack[0] if topic_stack else None),
        topic_stack=topic_stack,
        intent=context.intent,
        depth=context.depth,
        last_activity=context.last_activity,
    )


def merge_contexts(
    session: ConversationContext, persisted: ConversationContext
) -> ConversationContext:
    """Combine an in-session context with a persisted one.

    Session values win on every field that represents a "right now"
    signal (``current_topic``, ``intent``, ``depth``, ``last_activity``).
    The persisted ``topic_stack`` supplies trailing entries so a brand
    new session with an empty log still surfaces the user's prior
    focus, bounded by ``MAX_TOPIC_STACK``.
    """
    topic_stack = list(session.topic_stack)
    for topic in persisted.topic_stack:
        if topic not in topic_stack and len(topic_stack) < MAX_TOPIC_STACK:
            topic_stack.append(topic)
    return ConversationContext(
        current_topic=session.current_topic or persisted.current_topic,
        topic_stack=topic_stack,
        intent=session.intent or persisted.intent,
        depth=session.depth,
        last_activity=session.last_activity,
    )
