from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from app.security import redact_sensitive_text


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


class DynamoDatabase:
    """DynamoDB-backed runtime storage used by the production Lambdas.

    Static knowledge stays in the immutable container image. DynamoDB stores only
    short-lived conversation state, delivery idempotency, and compact audit events.
    """

    def __init__(
        self,
        table_name: str,
        *,
        region_name: str = "eu-central-1",
        retention_days: int = 7,
        table: Any | None = None,
    ):
        self.table_name = table_name
        self.retention_days = max(1, retention_days)
        self.table = table or boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    def _expires_at(self, days: int | None = None) -> int:
        return int(time.time()) + (days or self.retention_days) * 86400

    def init_schema(self) -> None:
        # CloudFormation owns the table schema and TTL configuration.
        return None

    def upsert_session(self, session_id: str) -> None:
        now = _utcnow()
        self.table.update_item(
            Key={"pk": f"SESSION#{session_id}", "sk": "META"},
            UpdateExpression=(
                "SET created_at = if_not_exists(created_at, :now), "
                "updated_at = :now, expires_at = :expires_at, entity_type = :entity_type"
            ),
            ExpressionAttributeValues={
                ":now": now,
                ":expires_at": self._expires_at(),
                ":entity_type": "chat_session",
            },
        )

    def add_message(self, session_id: str, role: str, content: str) -> None:
        self.upsert_session(session_id)
        now = _utcnow()
        self.table.put_item(
            Item={
                "pk": f"SESSION#{session_id}",
                "sk": f"MSG#{now}#{uuid.uuid4().hex}",
                "entity_type": "chat_message",
                "role": role,
                "content": redact_sensitive_text(content),
                "created_at": now,
                "expires_at": self._expires_at(),
            }
        )

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        response = self.table.get_item(Key={"pk": f"SESSION#{session_id}", "sk": "META"})
        item = response.get("Item")
        if not item:
            return None
        return {
            "session_id": session_id,
            "created_at": item["created_at"],
            "updated_at": item["updated_at"],
        }

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        response = self.table.query(
            KeyConditionExpression=Key("pk").eq(f"SESSION#{session_id}") & Key("sk").begins_with("MSG#"),
            ConsistentRead=True,
        )
        return [
            {
                "role": item["role"],
                "content": item["content"],
                "created_at": item["created_at"],
            }
            for item in response.get("Items", [])
        ]

    def prune_chat_history(self, retention_days: int) -> list[str]:
        del retention_days
        # DynamoDB TTL removes expired sessions and messages without table scans.
        return []

    def log_tool_call(
        self,
        session_id: str | None,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_output: dict[str, Any],
        success: bool,
        error: str | None = None,
    ) -> None:
        now = _utcnow()
        compact_input = redact_sensitive_text(json.dumps(tool_input, ensure_ascii=False, default=str))
        self.table.put_item(
            Item={
                "pk": f"AUDIT#{now[:10]}",
                "sk": f"TOOL#{now}#{uuid.uuid4().hex}",
                "entity_type": "tool_call",
                "session_id": session_id or "",
                "tool_name": tool_name,
                "tool_input": compact_input[:4000],
                "output_keys": sorted(str(key) for key in tool_output),
                "found": bool(tool_output.get("found")),
                "success": bool(success),
                "error": str(error or "")[:500],
                "created_at": now,
                "expires_at": self._expires_at(30),
            }
        )

    def get_conflicts(self) -> list[dict[str, Any]]:
        # Production knowledge is file-backed and conflicts are resolved pre-deploy.
        return []

    def claim_inbound_message(self, message_id: str) -> bool:
        now = _utcnow()
        try:
            self.table.put_item(
                Item={
                    "pk": f"INBOUND#{message_id}",
                    "sk": "META",
                    "entity_type": "whatsapp_inbound",
                    "status": "processing",
                    "created_at": now,
                    "updated_at": now,
                    "expires_at": self._expires_at(14),
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def get_inbound_message(self, message_id: str) -> dict[str, Any] | None:
        return self.table.get_item(
            Key={"pk": f"INBOUND#{message_id}", "sk": "META"},
            ConsistentRead=True,
        ).get("Item")

    def store_inbound_response(self, message_id: str, response_text: str) -> None:
        self.table.update_item(
            Key={"pk": f"INBOUND#{message_id}", "sk": "META"},
            UpdateExpression="SET #status = :status, response_text = :response, updated_at = :now",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": "response_ready",
                ":response": redact_sensitive_text(response_text),
                ":now": _utcnow(),
            },
        )

    def mark_inbound_sent(self, message_id: str) -> None:
        self.table.update_item(
            Key={"pk": f"INBOUND#{message_id}", "sk": "META"},
            UpdateExpression="SET #status = :status, updated_at = :now REMOVE response_text",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": "sent", ":now": _utcnow()},
        )

    def release_inbound_message(self, message_id: str) -> None:
        try:
            self.table.delete_item(
                Key={"pk": f"INBOUND#{message_id}", "sk": "META"},
                ConditionExpression="#status = :processing",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":processing": "processing"},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
