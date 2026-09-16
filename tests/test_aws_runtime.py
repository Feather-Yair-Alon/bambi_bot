from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

from botocore.exceptions import ClientError

from app.aws import webhook, worker
from app.aws.whatsapp_handoff import (
    build_contact_message,
    build_handoff_link,
    build_handoff_summary,
    find_approved_handoff_contact,
)
from app.aws.dynamodb_db import DynamoDatabase
from app.config import Settings


class FakeSqs:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def send_message(self, **kwargs: Any) -> None:
        self.messages.append(kwargs)


class FakeTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def update_item(self, *, Key: dict[str, str], ExpressionAttributeValues: dict[str, Any], **kwargs: Any) -> None:
        key = (Key["pk"], Key["sk"])
        item = self.items.setdefault(key, dict(Key))
        now = ExpressionAttributeValues.get(":now")
        item.setdefault("created_at", now)
        item["updated_at"] = now
        item["expires_at"] = ExpressionAttributeValues.get(":expires_at", item.get("expires_at"))
        if ":entity_type" in ExpressionAttributeValues:
            item["entity_type"] = ExpressionAttributeValues[":entity_type"]

    def put_item(self, *, Item: dict[str, Any], ConditionExpression: str | None = None) -> None:
        key = (Item["pk"], Item["sk"])
        if ConditionExpression and key in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                "PutItem",
            )
        self.items[key] = dict(Item)

    def get_item(self, *, Key: dict[str, str], **kwargs: Any) -> dict[str, Any]:
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": item} if item else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {
            "Items": sorted(
                (item for item in self.items.values() if item["sk"].startswith("MSG#")),
                key=lambda item: item["sk"],
            )
        }


def _event(method: str, path: str, *, body: str = "", headers: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": method}},
        "headers": headers or {},
        "body": body,
        "isBase64Encoded": False,
    }


def test_webhook_health_does_not_require_secrets() -> None:
    response = webhook.handler(_event("GET", "/health"), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["status"] == "ok"


def test_webhook_verification_requires_exact_token(monkeypatch) -> None:
    monkeypatch.setattr(webhook, "get_runtime_secret", lambda: {"meta_verify_token": "expected"})
    event = _event("GET", "/webhook")
    event["queryStringParameters"] = {
        "hub.mode": "subscribe",
        "hub.verify_token": "expected",
        "hub.challenge": "challenge-value",
    }

    response = webhook.handler(event, None)

    assert response["statusCode"] == 200
    assert response["body"] == "challenge-value"


def test_webhook_verification_fails_closed_without_configured_token(monkeypatch) -> None:
    monkeypatch.setattr(webhook, "get_runtime_secret", lambda: {"meta_verify_token": ""})
    event = _event("GET", "/webhook")
    event["queryStringParameters"] = {
        "hub.mode": "subscribe",
        "hub.verify_token": "",
        "hub.challenge": "challenge-value",
    }

    response = webhook.handler(event, None)

    assert response["statusCode"] == 403


def test_webhook_rejects_bad_signature_without_enqueuing(monkeypatch) -> None:
    sqs = FakeSqs()
    monkeypatch.setattr(webhook, "get_runtime_secret", lambda: {"meta_app_secret": "app-secret"})
    monkeypatch.setattr(webhook, "_sqs_client", lambda: sqs)

    response = webhook.handler(
        _event("POST", "/webhook", body='{"object":"whatsapp_business_account"}', headers={"x-hub-signature-256": "sha256=bad"}),
        None,
    )

    assert response["statusCode"] == 401
    assert sqs.messages == []


def test_webhook_enqueues_valid_signed_payload(monkeypatch) -> None:
    sqs = FakeSqs()
    payload = {"object": "whatsapp_business_account", "entry": []}
    body = json.dumps(payload, separators=(",", ":"))
    digest = hmac.new(b"app-secret", body.encode(), hashlib.sha256).hexdigest()
    monkeypatch.setenv("INBOUND_QUEUE_URL", "https://sqs.example/queue")
    monkeypatch.setattr(webhook, "get_runtime_secret", lambda: {"meta_app_secret": "app-secret"})
    monkeypatch.setattr(webhook, "_sqs_client", lambda: sqs)

    response = webhook.handler(
        _event("POST", "/webhook", body=body, headers={"X-Hub-Signature-256": f"sha256={digest}"}),
        None,
    )

    assert response["statusCode"] == 200
    assert len(sqs.messages) == 1
    assert json.loads(sqs.messages[0]["MessageBody"]) == payload


def test_worker_extracts_only_text_messages() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"id": "wamid.1", "from": "972500000000", "type": "text", "text": {"body": "שלום"}},
                                {"id": "wamid.2", "from": "972500000000", "type": "image", "image": {"id": "1"}},
                            ]
                        }
                    }
                ]
            }
        ]
    }

    assert list(worker._incoming_messages(payload)) == [
        {"message_id": "wamid.1", "from": "972500000000", "text": "שלום"}
    ]


def test_worker_formats_follow_up_once() -> None:
    answer = SimpleNamespace(answer="אפשר לעזור.", follow_up_question="איזה קורס מעניין אותך?")

    assert worker._answer_text(answer) == "אפשר לעזור.\n\nאיזה קורס מעניין אותך?"


def test_worker_converts_markdown_to_whatsapp_plain_text() -> None:
    answer = SimpleNamespace(
        answer=(
            "## פרטי הקורס\n\n"
            "**משך:** 8 שעות\n"
            "- נושא ראשון\n"
            "- [מידע נוסף](https://example.com/course)\n"
            "`אין להציג קוד`"
        ),
        follow_up_question=None,
    )

    assert worker._answer_text(answer) == (
        "פרטי הקורס\n\n"
        "משך: 8 שעות\n"
        "• נושא ראשון\n"
        "• מידע נוסף: https://example.com/course\n"
        "אין להציג קוד"
    )


async def test_worker_typing_indicator_uses_short_timeout(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_meta_post(
        secret: dict[str, Any], payload: dict[str, Any], *, timeout_seconds: float = 20.0
    ) -> None:
        captured.update(
            secret=secret,
            payload=payload,
            timeout_seconds=timeout_seconds,
        )

    monkeypatch.setattr(worker, "_meta_post", fake_meta_post)

    await worker._send_typing({"meta_access_token": "token"}, "wamid.1")

    assert captured["timeout_seconds"] == 3.0
    assert captured["payload"]["typing_indicator"] == {"type": "text"}


def test_handoff_matches_only_one_approved_contact() -> None:
    contact = find_approved_handoff_contact(
        "אני מעבירה אותך למיקה, רכזת תחום עבודה בגובה: 054-940-5419"
    )

    assert contact is not None
    assert contact.owner.startswith("מיקה")
    assert find_approved_handoff_contact("אפשר לפנות למספר 050-000-0000") is None
    assert find_approved_handoff_contact("מחיר 549 ש״ח, מועד 40.54.19") is None
    assert (
        find_approved_handoff_contact("אפשר לפנות למיקה 054-940-5419 או לחן 054-904-7872")
        is None
    )


def test_handoff_contact_card_and_link_use_whatsapp_number() -> None:
    contact = find_approved_handoff_contact("פנו לאליאור: 0549688028")
    assert contact is not None

    payload = build_contact_message(contact, "972501234567")
    link = build_handoff_link(
        contact,
        [{"role": "user", "content": "אני רוצה להירשם לקורס מלגזה"}],
    )

    assert payload["type"] == "contacts"
    assert payload["contacts"][0]["name"]["first_name"] == "אליאור"
    assert payload["contacts"][0]["phones"][0]["wa_id"] == "972549688028"
    parsed = urlparse(link)
    assert parsed.netloc == "wa.me"
    assert parsed.path == "/972549688028"
    assert "קורס מלגזה" in parse_qs(parsed.query)["text"][0]


def test_safety_controller_handoff_uses_tali_contact_and_whatsapp_link() -> None:
    contact = find_approved_handoff_contact(
        "להמשך בירור בקורס בקר בטיחות אפשר לפנות לטלי: 052-702-3884"
    )

    assert contact is not None
    assert contact.owner.startswith("טלי")
    assert contact.phone == "052-702-3884"

    link = build_handoff_link(
        contact,
        [{"role": "user", "content": "אני רוצה פרטים על קורס בקר בטיחות"}],
    )

    parsed = urlparse(link)
    assert parsed.netloc == "wa.me"
    assert parsed.path == "/972527023884"
    assert "קורס בקר בטיחות" in parse_qs(parsed.query)["text"][0]


def test_handoff_summary_is_compact_and_removes_personal_details() -> None:
    history = [
        {"role": "user", "content": "שלום"},
        {"role": "user", "content": "אני צריך קורס עבודה בגובה"},
        {"role": "assistant", "content": "איזה נושא?"},
        {
            "role": "user",
            "content": "כן, הטלפון 054-123-4567 והמייל student@example.com ותז 123456782",
        },
    ]

    summary = build_handoff_summary("מיקה", history)

    assert "אני צריך קורס עבודה בגובה" in summary
    assert "054-123-4567" not in summary
    assert "student@example.com" not in summary
    assert "123456782" not in summary
    assert "[PHONE_REDACTED]" not in summary


async def test_worker_sends_contact_and_prefilled_link_after_handoff(monkeypatch) -> None:
    payloads: list[dict[str, Any]] = []

    async def fake_meta_post(
        secret: dict[str, Any], payload: dict[str, Any], *, timeout_seconds: float = 20.0
    ) -> None:
        del secret, timeout_seconds
        payloads.append(payload)

    monkeypatch.setattr(worker, "_meta_post", fake_meta_post)

    await worker._send_response_bundle(
        {"meta_access_token": "token", "meta_phone_number_id": "phone-id"},
        "972501234567",
        "נדרש המשך טיפול מול מיקה: 054-940-5419",
        [{"role": "user", "content": "אני צריך קורס עבודה בגובה"}],
    )

    assert [payload["type"] for payload in payloads] == ["text", "contacts", "text"]
    assert "wa.me/972549405419" in payloads[-1]["text"]["body"]


async def test_handoff_contact_failure_does_not_block_link(monkeypatch) -> None:
    sent_types: list[str] = []

    async def fake_meta_post(
        secret: dict[str, Any], payload: dict[str, Any], *, timeout_seconds: float = 20.0
    ) -> None:
        del secret, timeout_seconds
        sent_types.append(payload["type"])
        if payload["type"] == "contacts":
            raise RuntimeError("contact messages unavailable")

    monkeypatch.setattr(worker, "_meta_post", fake_meta_post)

    await worker._send_handoff_extras(
        {"meta_access_token": "token", "meta_phone_number_id": "phone-id"},
        "972501234567",
        "נדרש המשך טיפול מול מיקה: 054-940-5419",
        [{"role": "user", "content": "אני צריך קורס עבודה בגובה"}],
    )

    assert sent_types == ["contacts", "text"]


def test_dynamodb_runtime_sessions_and_message_claims() -> None:
    table = FakeTable()
    db = DynamoDatabase("test", table=table)

    db.upsert_session("session-1")
    db.add_message("session-1", "user", "שלום")

    assert db.get_session("session-1")["session_id"] == "session-1"
    assert db.get_messages("session-1")[0]["content"] == "שלום"
    assert db.claim_inbound_message("wamid.1") is True
    assert db.claim_inbound_message("wamid.1") is False


def test_dynamodb_settings_require_table_name(tmp_path) -> None:
    try:
        Settings(
            _env_file=None,
            LLM_PROVIDER="anthropic",
            ANTHROPIC_API_KEY="test",
            STORAGE_BACKEND="dynamodb",
            DATA_DIR=str(tmp_path),
        )
    except ValueError as exc:
        assert "DYNAMODB_TABLE_NAME" in str(exc)
    else:
        raise AssertionError("DynamoDB storage accepted without a table name")
