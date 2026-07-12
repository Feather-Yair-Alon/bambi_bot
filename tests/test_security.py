from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from agents.exceptions import OutputGuardrailTripwireTriggered

from app.db import Database
from app.security import (
    RedactingSession,
    SlidingWindowRateLimiter,
    redact_sensitive_text,
    sanitize_persisted_history,
)
from app.services.agent_service import AgentService


def test_redact_sensitive_text_removes_registration_details() -> None:
    text = "054-123-4567, 312496730, student@example.com"

    redacted = redact_sensitive_text(text)

    assert "054-123-4567" not in redacted
    assert "312496730" not in redacted
    assert "student@example.com" not in redacted


@pytest.mark.asyncio
async def test_redacting_session_sanitizes_items_before_persistence() -> None:
    class FakeSession:
        session_id = "session1"
        session_settings = None

        def __init__(self):
            self.items = []

        async def get_items(self, _limit=None):
            return self.items

        async def add_items(self, items):
            self.items.extend(items)

        async def pop_item(self):
            return self.items.pop() if self.items else None

        async def clear_session(self):
            self.items.clear()

    raw = FakeSession()
    session = RedactingSession(raw)
    message_id = "msg_07965ea1898ac6a8006a53f7bd8b488194a312496730ee76f3"
    await session.add_items(
        [{"id": message_id, "role": "user", "content": "ת.ז. 312496730, student@example.com"}]
    )

    assert "312496730" not in raw.items[0]["content"]
    assert "student@example.com" not in raw.items[0]["content"]
    assert raw.items[0]["id"] == message_id


@pytest.mark.asyncio
async def test_rate_limiter_blocks_requests_above_limit() -> None:
    limiter = SlidingWindowRateLimiter(2)

    assert await limiter.allow("client") is True
    assert await limiter.allow("client") is True
    assert await limiter.allow("client") is False
    assert await limiter.allow("other-client") is True


@pytest.mark.asyncio
async def test_stream_releases_text_immediately_even_if_output_guardrail_later_blocks(tmp_path, monkeypatch) -> None:
    db = Database(tmp_path / "app.db")
    db.init_schema()
    db.upsert_session("session1")
    settings = SimpleNamespace(
        session_db_path=tmp_path / "sessions.db",
        mybusiness_app_id="",
        mybusiness_master_key="",
        mybusiness_base_url="https://example.test/parse",
        mybusiness_timeout_seconds=1,
    )
    knowledge_files = SimpleNamespace(tool_specs=lambda: [])
    service = AgentService(settings, db, knowledge_files)
    monkeypatch.setattr(service, "_get_streaming_agent", lambda: object())

    class FakeResult:
        final_output = ""

        async def stream_events(self):
            yield SimpleNamespace(
                type="raw_response_event",
                data=SimpleNamespace(type="response.output_text.delta", delta="unsafe leaked text"),
            )
            raise OutputGuardrailTripwireTriggered(SimpleNamespace(guardrail=object()))

    monkeypatch.setattr(
        "app.services.agent_service.Runner.run_streamed",
        lambda *_args, **_kwargs: FakeResult(),
    )

    events = [event async for event in service.ask_stream("session1", "שלום")]

    assert any(event.get("type") == "delta" and event.get("delta") == "unsafe leaked text" for event in events)
    assert events[-1]["type"] == "final"
    assert events[-1]["response"]["needs_human_review"] is True


@pytest.mark.asyncio
async def test_stream_returns_final_event_on_unexpected_provider_error(tmp_path, monkeypatch) -> None:
    db = Database(tmp_path / "app.db")
    db.init_schema()
    db.upsert_session("session1")
    settings = SimpleNamespace(
        session_db_path=tmp_path / "sessions.db",
        mybusiness_app_id="",
        mybusiness_master_key="",
        mybusiness_base_url="https://example.test/parse",
        mybusiness_timeout_seconds=1,
    )
    service = AgentService(settings, db, SimpleNamespace(tool_specs=lambda: []))
    monkeypatch.setattr(service, "_get_streaming_agent", lambda: object())

    class FailedResult:
        final_output = ""

        async def stream_events(self):
            raise RuntimeError("provider failed")
            yield  # pragma: no cover

    monkeypatch.setattr(
        "app.services.agent_service.Runner.run_streamed",
        lambda *_args, **_kwargs: FailedResult(),
    )

    events = [event async for event in service.ask_stream("session1", "שלום")]

    assert events[-1]["type"] == "final"
    assert events[-1]["response"]["needs_human_review"] is True


def test_prune_chat_history_removes_expired_sessions(tmp_path) -> None:
    db = Database(tmp_path / "app.db")
    db.init_schema()
    db.upsert_session("old")
    db.add_message("old", "user", "message")
    old_timestamp = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    with db.connection() as conn:
        conn.execute("UPDATE chat_sessions SET updated_at=? WHERE session_id='old'", (old_timestamp,))

    removed = db.prune_chat_history(7)

    assert removed == ["old"]
    assert db.get_session("old") is None


def test_sanitize_persisted_history_redacts_existing_messages(tmp_path) -> None:
    db_path = tmp_path / "app.db"
    db = Database(db_path)
    db.init_schema()
    db.add_message("session1", "user", "312496730 student@example.com")

    sanitize_persisted_history(db_path, tmp_path / "missing-agent.db")

    content = db.get_messages("session1")[0]["content"]
    assert "312496730" not in content
    assert "student@example.com" not in content


def test_sanitize_agent_history_preserves_ids_and_drops_corrupted_sessions(tmp_path) -> None:
    app_db_path = tmp_path / "app.db"
    Database(app_db_path).init_schema()
    agent_db_path = tmp_path / "agent.db"
    valid_id = "msg_07965ea1898ac6a8006a53f7bd8b488194a312496730ee76f3"
    with sqlite3.connect(agent_db_path) as conn:
        conn.execute("CREATE TABLE agent_sessions (session_id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE agent_messages (id INTEGER PRIMARY KEY, session_id TEXT, message_data TEXT)"
        )
        conn.executemany("INSERT INTO agent_sessions(session_id) VALUES(?)", [("valid",), ("broken",)])
        conn.execute(
            "INSERT INTO agent_messages(session_id, message_data) VALUES(?, ?)",
            (
                "valid",
                json.dumps({"id": valid_id, "role": "user", "content": "312496730 student@example.com"}),
            ),
        )
        conn.execute(
            "INSERT INTO agent_messages(session_id, message_data) VALUES(?, ?)",
            ("broken", json.dumps({"id": "msg_abc[ID_REDACTED]xyz", "role": "assistant"})),
        )

    sanitize_persisted_history(app_db_path, agent_db_path)

    with sqlite3.connect(agent_db_path) as conn:
        valid_payload = json.loads(
            conn.execute("SELECT message_data FROM agent_messages WHERE session_id='valid'").fetchone()[0]
        )
        broken_count = conn.execute(
            "SELECT COUNT(*) FROM agent_messages WHERE session_id='broken'"
        ).fetchone()[0]
    assert valid_payload["id"] == valid_id
    assert "312496730" not in valid_payload["content"]
    assert "student@example.com" not in valid_payload["content"]
    assert broken_count == 0
