from __future__ import annotations

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
    await session.add_items([{"role": "user", "content": "ת.ז. 312496730, student@example.com"}])

    assert "312496730" not in str(raw.items)
    assert "student@example.com" not in str(raw.items)


@pytest.mark.asyncio
async def test_rate_limiter_blocks_requests_above_limit() -> None:
    limiter = SlidingWindowRateLimiter(2)

    assert await limiter.allow("client") is True
    assert await limiter.allow("client") is True
    assert await limiter.allow("client") is False
    assert await limiter.allow("other-client") is True


@pytest.mark.asyncio
async def test_stream_does_not_release_text_before_output_guardrail_passes(tmp_path, monkeypatch) -> None:
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

    assert not any(event.get("type") == "delta" for event in events)
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
