from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Iterator

import httpx

from app.aws.dynamodb_db import DynamoDatabase
from app.aws.secrets import get_runtime_secret

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
_runtime_configured = False


def _configure_runtime() -> dict[str, Any]:
    global _runtime_configured
    secret = get_runtime_secret()
    if not _runtime_configured:
        mappings = {
            "ANTHROPIC_API_KEY": "anthropic_api_key",
            "MYBUSINESS_APP_ID": "mybusiness_app_id",
            "MYBUSINESS_MASTER_KEY": "mybusiness_master_key",
            "MYBUSINESS_BASE_URL": "mybusiness_base_url",
        }
        for env_name, secret_name in mappings.items():
            value = secret.get(secret_name)
            if value:
                os.environ[env_name] = str(value)
        os.environ["LLM_PROVIDER"] = "anthropic"

        from app.config import get_settings
        from app.dependencies import get_agent_service, get_db

        get_settings.cache_clear()
        get_db.cache_clear()
        get_agent_service.cache_clear()
        _runtime_configured = True
    return secret


def _incoming_messages(payload: dict[str, Any]) -> Iterator[dict[str, str]]:
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for message in value.get("messages") or []:
                text = (message.get("text") or {}).get("body")
                if message.get("type") == "text" and text:
                    yield {
                        "message_id": str(message.get("id") or ""),
                        "from": str(message.get("from") or ""),
                        "text": str(text),
                    }


def _whatsapp_plain_text(value: str) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"```(?:[A-Za-z0-9_+-]+)?\n?", "", text)
    text = text.replace("```", "").replace("`", "")
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1: \2", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "• ", text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_\n]+)__", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", text)
    text = re.sub(r"~~([^~\n]+)~~", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _answer_text(answer: Any) -> str:
    text = str(answer.answer or "").strip()
    follow_up = str(answer.follow_up_question or "").strip()
    if follow_up and follow_up not in text:
        text = f"{text}\n\n{follow_up}".strip()
    return _whatsapp_plain_text(text)


async def _meta_post(
    secret: dict[str, Any], payload: dict[str, Any], *, timeout_seconds: float = 20.0
) -> None:
    token = str(secret.get("meta_access_token") or "")
    phone_number_id = str(secret.get("meta_phone_number_id") or "")
    if not token or not phone_number_id:
        raise RuntimeError("Meta access token or phone number ID is not configured")
    version = os.environ.get("META_GRAPH_API_VERSION", "v26.0")
    url = f"https://graph.facebook.com/{version}/{phone_number_id}/messages"
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(
            url,
            headers={"authorization": f"Bearer {token}"},
            json=payload,
        )
        if response.is_error:
            logger.warning(
                "Meta API request failed status=%s error=%s",
                response.status_code,
                response.text[:1000],
            )
        response.raise_for_status()


async def _send_typing(secret: dict[str, Any], message_id: str) -> None:
    await _meta_post(
        secret,
        {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
            "typing_indicator": {"type": "text"},
        },
        timeout_seconds=3.0,
    )


async def _send_typing_safely(
    secret: dict[str, Any], message_id: str, started: float
) -> None:
    try:
        await _send_typing(secret, message_id)
        logger.info(
            "WhatsApp typing indicator sent elapsed_ms=%s",
            round((time.perf_counter() - started) * 1000),
        )
    except Exception:
        logger.warning("Could not send WhatsApp typing indicator", exc_info=True)


async def _send_text(secret: dict[str, Any], recipient: str, text: str) -> None:
    chunks = [text[index : index + 4000] for index in range(0, len(text), 4000)] or [""]
    for chunk in chunks:
        await _meta_post(
            secret,
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient,
                "type": "text",
                "text": {"preview_url": False, "body": chunk},
            },
        )


async def _process_message(message: dict[str, str], secret: dict[str, Any]) -> None:
    from app.dependencies import get_agent_service, get_db

    db = get_db()
    if not isinstance(db, DynamoDatabase):
        raise RuntimeError("WhatsApp worker requires DynamoDB storage")

    message_id = message["message_id"]
    existing = db.get_inbound_message(message_id)
    if existing and existing.get("status") == "sent":
        return
    if existing and existing.get("status") == "response_ready":
        await _send_text(secret, message["from"], str(existing.get("response_text") or ""))
        db.mark_inbound_sent(message_id)
        return
    if existing or not db.claim_inbound_message(message_id):
        return

    started = time.perf_counter()
    try:
        typing_task = asyncio.create_task(_send_typing_safely(secret, message_id, started))
        await asyncio.sleep(0)

        session_id = f"whatsapp:{message['from']}"
        db.upsert_session(session_id)
        agent_started = time.perf_counter()
        answer = await get_agent_service().ask(session_id, message["text"])
        logger.info(
            "WhatsApp agent completed duration_ms=%s total_elapsed_ms=%s",
            round((time.perf_counter() - agent_started) * 1000),
            round((time.perf_counter() - started) * 1000),
        )
        response_text = _answer_text(answer)
        db.store_inbound_response(message_id, response_text)
        await _send_text(secret, message["from"], response_text)
        db.mark_inbound_sent(message_id)
        logger.info(
            "WhatsApp response sent total_elapsed_ms=%s",
            round((time.perf_counter() - started) * 1000),
        )
        if not typing_task.done():
            typing_task.cancel()
        await asyncio.gather(typing_task, return_exceptions=True)
    except Exception:
        try:
            db.release_inbound_message(message_id)
        except Exception:
            logger.warning("Could not release failed inbound message", exc_info=True)
        raise


async def _process_record(record: dict[str, Any]) -> None:
    secret = _configure_runtime()
    payload = json.loads(record.get("body") or "{}")
    for message in _incoming_messages(payload):
        if message["message_id"] and message["from"]:
            await _process_message(message, secret)


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    failures: list[dict[str, str]] = []
    for record in event.get("Records") or []:
        try:
            asyncio.run(_process_record(record))
        except Exception:
            logger.exception("WhatsApp queue record failed")
            failures.append({"itemIdentifier": str(record.get("messageId") or "")})
    return {"batchItemFailures": failures}
