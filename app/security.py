from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+972[- ]?|0)(?:5\d|[23489])[- ]?\d{3}[- ]?\d{4}(?!\d)")
ISRAELI_ID_RE = re.compile(r"(?<!\d)\d{9}(?!\d)")


def redact_sensitive_text(value: str) -> str:
    text = EMAIL_RE.sub("[EMAIL_REDACTED]", str(value))
    text = PHONE_RE.sub("[PHONE_REDACTED]", text)
    return ISRAELI_ID_RE.sub("[ID_REDACTED]", text)


def redact_session_item(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, list):
        return [redact_session_item(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_session_item(item) for key, item in value.items()}
    return value


class RedactingSession:
    """Persist conversation context without raw phone, email, or ID values."""

    def __init__(self, session: Any):
        self._session = session
        self.session_id = session.session_id
        self.session_settings = session.session_settings

    async def get_items(self, limit: int | None = None) -> list[Any]:
        return await self._session.get_items(limit)

    async def add_items(self, items: list[Any]) -> None:
        await self._session.add_items([redact_session_item(item) for item in items])

    async def pop_item(self) -> Any | None:
        return await self._session.pop_item()

    async def clear_session(self) -> None:
        await self._session.clear_session()


class SlidingWindowRateLimiter:
    def __init__(self, requests_per_minute: int):
        self.limit = max(1, requests_per_minute)
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - 60
        async with self._lock:
            requests = self._requests[key]
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if len(requests) >= self.limit:
                return False
            requests.append(now)
            return True


def prune_agent_sessions(path: Path, session_ids: list[str]) -> None:
    if not session_ids or not path.exists():
        return
    placeholders = ",".join("?" for _ in session_ids)
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"agent_messages", "agent_sessions"}.issubset(tables):
            return
        conn.execute(f"DELETE FROM agent_messages WHERE session_id IN ({placeholders})", session_ids)
        conn.execute(f"DELETE FROM agent_sessions WHERE session_id IN ({placeholders})", session_ids)


def sanitize_persisted_history(app_db_path: Path, agent_db_path: Path) -> None:
    _sanitize_text_column(app_db_path, "chat_messages", "id", "content")
    _sanitize_text_column(agent_db_path, "agent_messages", "id", "message_data")


def _sanitize_text_column(path: Path, table: str, id_column: str, text_column: str) -> None:
    if not path.exists():
        return
    with sqlite3.connect(path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not exists:
            return
        rows = conn.execute(f"SELECT {id_column}, {text_column} FROM {table}").fetchall()
        updates = []
        for row_id, value in rows:
            redacted = redact_sensitive_text(str(value or ""))
            if redacted != value:
                updates.append((redacted, row_id))
        if updates:
            conn.executemany(
                f"UPDATE {table} SET {text_column}=? WHERE {id_column}=?",
                updates,
            )
