from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from functools import lru_cache
from typing import Any

import boto3

from app.aws.secrets import get_runtime_secret

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _sqs_client():
    return boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "eu-central-1"))


def _response(status_code: int, body: str, content_type: str = "application/json") -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"content-type": content_type, "cache-control": "no-store"},
        "body": body,
    }


def _raw_body(event: dict[str, Any]) -> bytes:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return str(body).encode("utf-8")


def _header(event: dict[str, Any], name: str) -> str:
    expected = name.lower()
    for key, value in (event.get("headers") or {}).items():
        if str(key).lower() == expected:
            return str(value)
    return ""


def _valid_signature(body: bytes, signature: str, app_secret: str) -> bool:
    if not signature.startswith("sha256=") or not app_secret:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.removeprefix("sha256="), expected)


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    path = event.get("rawPath") or event.get("path") or ""
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or ""
    ).upper()

    if path == "/health" and method == "GET":
        return _response(200, json.dumps({"status": "ok", "service": "bambi-whatsapp-webhook"}))

    secret = get_runtime_secret()
    if method == "GET" and path == "/webhook":
        query = event.get("queryStringParameters") or {}
        configured_token = str(secret.get("meta_verify_token") or "")
        provided_token = str(query.get("hub.verify_token") or "")
        verified = (
            bool(configured_token)
            and bool(provided_token)
            and query.get("hub.mode") == "subscribe"
            and hmac.compare_digest(provided_token, configured_token)
        )
        if not verified:
            return _response(403, json.dumps({"error": "verification_failed"}))
        return _response(200, str(query.get("hub.challenge") or ""), "text/plain")

    if method != "POST" or path != "/webhook":
        return _response(404, json.dumps({"error": "not_found"}))

    body = _raw_body(event)
    if not _valid_signature(body, _header(event, "x-hub-signature-256"), str(secret.get("meta_app_secret") or "")):
        return _response(401, json.dumps({"error": "invalid_signature"}))

    try:
        payload = json.loads(body)
        if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
            return _response(400, json.dumps({"error": "invalid_payload"}))
        _sqs_client().send_message(
            QueueUrl=os.environ["INBOUND_QUEUE_URL"],
            MessageBody=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        logger.exception("Rejected invalid WhatsApp webhook payload")
        return _response(400, json.dumps({"error": "invalid_payload"}))
    except Exception:
        logger.exception("Failed to enqueue WhatsApp webhook")
        return _response(503, json.dumps({"error": "temporarily_unavailable"}))

    return _response(200, json.dumps({"status": "accepted"}))
