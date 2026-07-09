"""Tests for the persistent ConversationContext store."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from qracer.conversation.context import ConversationContext
from qracer.conversation.context_store import (
    DEFAULT_STALE_DAYS,
    decay_stale,
    load_context,
    merge_contexts,
    merge_with_theses,
    save_context,
)


class TestSaveLoadRoundTrip:
    def test_roundtrip_preserves_all_fields(self, tmp_path):
        activity = datetime(2026, 4, 10, 9, 30, 0)
        ctx = ConversationContext(
            current_topic="AAPL",
            topic_stack=["AAPL", "TSLA", "NVDA"],
            intent="research",
            depth="deep",
            last_activity=activity,
        )
        path = tmp_path / "context.json"

        save_context(ctx, path)
        loaded = load_context(path)

        assert loaded.current_topic == "AAPL"
        assert loaded.topic_stack == ["AAPL", "TSLA", "NVDA"]
        assert loaded.intent == "research"
        assert loaded.depth == "deep"
        assert loaded.last_activity == activity

    def test_save_creates_parent_directory(self, tmp_path):
        ctx = ConversationContext(current_topic="AAPL", topic_stack=["AAPL"])
        path = tmp_path / "nested" / "dir" / "context.json"

        save_context(ctx, path)

        assert path.exists()

    def test_save_is_valid_json(self, tmp_path):
        ctx = ConversationContext(current_topic="AAPL", topic_stack=["AAPL"])
        path = tmp_path / "context.json"

        save_context(ctx, path)
        data = json.loads(path.read_text())

        assert data["current_topic"] == "AAPL"
        assert data["topic_stack"] == ["AAPL"]


class TestLoadFallbacks:
    def test_missing_file_returns_empty_context(self, tmp_path):
        ctx = load_context(tmp_path / "missing.json")

        assert ctx.current_topic is None
        assert ctx.topic_stack == []
        assert ctx.intent is None
        assert ctx.depth == "quick"

    def test_malformed_json_returns_empty_context(self, tmp_path):
        path = tmp_path / "context.json"
        path.write_text("{ this is not json")

        ctx = load_context(path)

        assert ctx.current_topic is None
        assert ctx.topic_stack == []

    def test_non_dict_json_returns_empty_context(self, tmp_path):
        path = tmp_path / "context.json"
        path.write_text("[]")

        ctx = load_context(path)

        assert ctx.topic_stack == []

    def test_bad_timestamp_falls_back_to_now(self, tmp_path):
        path = tmp_path / "context.json"
        path.write_text(json.dumps({"topic_stack": ["AAPL"], "last_activity": "not-a-date"}))

        before = datetime.now()
        ctx = load_context(path)
        after = datetime.now()

        assert ctx.topic_stack == ["AAPL"]
        assert before <= ctx.last_activity <= after

    def test_non_string_topics_are_filtered(self, tmp_path):
        path = tmp_path / "context.json"
        path.write_text(
            json.dumps(
                {
                    "topic_stack": ["AAPL", 42, None, "TSLA"],
                    "last_activity": datetime.now().isoformat(),
                }
            )
        )

        ctx = load_context(path)

        assert ctx.topic_stack == ["AAPL", "TSLA"]


class TestDecayStale:
    def test_fresh_context_preserved(self):
        ctx = ConversationContext(
            current_topic="AAPL",
            topic_stack=["AAPL", "TSLA"],
            last_activity=datetime.now() - timedelta(days=1),
        )

        result = decay_stale(ctx)

        assert result.topic_stack == ["AAPL", "TSLA"]
        assert result.current_topic == "AAPL"

    def test_stale_context_clears_topics(self):
        stale_activity = datetime.now() - timedelta(days=DEFAULT_STALE_DAYS + 1)
        ctx = ConversationContext(
            current_topic="AAPL",
            topic_stack=["AAPL", "TSLA"],
            last_activity=stale_activity,
        )

        result = decay_stale(ctx)

        assert result.topic_stack == []
        assert result.current_topic is None
        # last_activity preserved so staleness can still be reported.
        assert result.last_activity == stale_activity

    def test_custom_max_age(self):
        ctx = ConversationContext(
            current_topic="AAPL",
            topic_stack=["AAPL"],
            last_activity=datetime.now() - timedelta(days=2),
        )

        kept = decay_stale(ctx, max_age_days=7)
        dropped = decay_stale(ctx, max_age_days=1)

        assert kept.topic_stack == ["AAPL"]
        assert dropped.topic_stack == []

    def test_empty_context_is_noop(self):
        ctx = ConversationContext(last_activity=datetime.now() - timedelta(days=30))

        result = decay_stale(ctx)

        assert result is ctx  # fast-path returns unchanged


class TestMergeWithTheses:
    def test_appends_new_thesis_tickers(self):
        ctx = ConversationContext(topic_stack=["AAPL"])

        result = merge_with_theses(ctx, ["NVDA", "TSLA"])

        assert result.topic_stack == ["AAPL", "NVDA", "TSLA"]

    def test_deduplicates(self):
        ctx = ConversationContext(topic_stack=["AAPL"])

        result = merge_with_theses(ctx, ["AAPL", "TSLA"])

        assert result.topic_stack == ["AAPL", "TSLA"]

    def test_respects_max_topic_stack(self):
        ctx = ConversationContext(topic_stack=["A", "B", "C", "D"])

        result = merge_with_theses(ctx, ["E", "F", "G"])

        assert len(result.topic_stack) == 5
        assert result.topic_stack == ["A", "B", "C", "D", "E"]

    def test_seeds_current_topic_from_stack(self):
        ctx = ConversationContext()

        result = merge_with_theses(ctx, ["NVDA", "TSLA"])

        assert result.current_topic == "NVDA"
        assert result.topic_stack == ["NVDA", "TSLA"]

    def test_existing_current_topic_preserved(self):
        ctx = ConversationContext(current_topic="AAPL", topic_stack=["AAPL"])

        result = merge_with_theses(ctx, ["NVDA"])

        assert result.current_topic == "AAPL"

    def test_empty_thesis_list_is_noop_for_stack(self):
        ctx = ConversationContext(current_topic="AAPL", topic_stack=["AAPL"])

        result = merge_with_theses(ctx, [])

        assert result.topic_stack == ["AAPL"]
        assert result.current_topic == "AAPL"


class TestMergeContexts:
    def test_session_takes_precedence_for_scalars(self):
        session = ConversationContext(
            current_topic="NVDA", intent="buy", depth="deep", topic_stack=["NVDA"]
        )
        persisted = ConversationContext(
            current_topic="AAPL", intent="research", depth="quick", topic_stack=["AAPL"]
        )

        result = merge_contexts(session, persisted)

        assert result.current_topic == "NVDA"
        assert result.intent == "buy"
        assert result.depth == "deep"

    def test_persisted_supplies_missing_scalars(self):
        session = ConversationContext()
        persisted = ConversationContext(
            current_topic="AAPL", intent="research", topic_stack=["AAPL"]
        )

        result = merge_contexts(session, persisted)

        assert result.current_topic == "AAPL"
        assert result.intent == "research"

    def test_topic_stacks_merge_session_first(self):
        session = ConversationContext(topic_stack=["NVDA", "TSLA"])
        persisted = ConversationContext(topic_stack=["AAPL", "TSLA", "GOOG"])

        result = merge_contexts(session, persisted)

        # TSLA is deduped, order is session-first then persisted.
        assert result.topic_stack == ["NVDA", "TSLA", "AAPL", "GOOG"]

    def test_merge_respects_max_topic_stack(self):
        session = ConversationContext(topic_stack=["A", "B", "C"])
        persisted = ConversationContext(topic_stack=["D", "E", "F", "G"])

        result = merge_contexts(session, persisted)

        assert len(result.topic_stack) == 5
        assert result.topic_stack == ["A", "B", "C", "D", "E"]

    def test_merge_keeps_session_last_activity(self):
        now = datetime.now()
        session = ConversationContext(last_activity=now)
        persisted = ConversationContext(last_activity=now - timedelta(days=3))

        result = merge_contexts(session, persisted)

        assert result.last_activity == now
