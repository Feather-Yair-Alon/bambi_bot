from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from app.security import REDACTION_MARKERS, redact_sensitive_text
from app.services.contact_channels import CONTACT_CHANNELS, ContactChannel


_GENERIC_REPLIES = {
    "אוקי",
    "בסדר",
    "היי",
    "כן",
    "לא",
    "מעולה",
    "שלום",
    "תודה",
}


def find_approved_handoff_contact(answer_text: str) -> ContactChannel | None:
    """Match only a specialist whose approved phone appears in the final answer."""
    candidates = {
        _digits(candidate)
        for candidate in re.findall(
            r"(?<!\d)(?:\+?972|0)[\d\s()\-]{8,16}(?!\d)",
            answer_text,
        )
    }
    matches = []
    for channel in CONTACT_CHANNELS:
        local = _digits(channel.phone)
        international = to_whatsapp_number(channel.phone)
        if local in candidates or international in candidates:
            matches.append(channel)
    return matches[0] if len(matches) == 1 else None


def build_contact_message(contact: ContactChannel, recipient: str) -> dict[str, Any]:
    first_name, title = _contact_name_parts(contact.owner)
    whatsapp_number = to_whatsapp_number(contact.phone)
    return {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "contacts",
        "contacts": [
            {
                "name": {
                    "formatted_name": contact.owner,
                    "first_name": first_name,
                },
                "org": {
                    "company": "מכללת במבי",
                    "department": contact.family,
                    "title": title,
                },
                "phones": [
                    {
                        "phone": f"+{whatsapp_number}",
                        "wa_id": whatsapp_number,
                        "type": "WORK",
                    }
                ],
            }
        ],
    }


def build_handoff_link(contact: ContactChannel, history: list[dict[str, Any]]) -> str:
    first_name, _ = _contact_name_parts(contact.owner)
    summary = build_handoff_summary(first_name, history)
    return f"https://wa.me/{to_whatsapp_number(contact.phone)}?text={quote(summary, safe='')}"


def build_handoff_link_message(contact: ContactChannel, history: list[dict[str, Any]]) -> str:
    first_name, _ = _contact_name_parts(contact.owner)
    return (
        f"למעבר ישיר לשיחה עם {first_name}, עם סיכום מוכן לשליחה:\n"
        f"{build_handoff_link(contact, history)}"
    )


def build_handoff_summary(first_name: str, history: list[dict[str, Any]]) -> str:
    user_messages = []
    for row in history:
        if str(row.get("role") or "") != "user":
            continue
        text = _safe_history_text(str(row.get("content") or ""))
        if not text or _is_generic_reply(text) or text in user_messages:
            continue
        user_messages.append(text)

    lines = [
        f"שלום {first_name},",
        "דנה ממכללת במבי הפנתה אותי אלייך להמשך טיפול.",
    ]
    if user_messages:
        lines.append("סיכום הפנייה:")
        lines.extend(f"• {message}" for message in user_messages[-3:])
    else:
        lines.append("אשמח לסיוע בנושא שעליו שוחחתי עם דנה.")
    lines.extend(
        [
            "דנה בדקה עבורי מידע ראשוני ונדרש המשך טיפול אנושי.",
            "אשמח לעזרתך.",
        ]
    )
    return "\n".join(lines)


def to_whatsapp_number(phone: str) -> str:
    digits = _digits(phone)
    if digits.startswith("0"):
        return f"972{digits[1:]}"
    return digits


def _safe_history_text(value: str) -> str:
    text = value
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        text = str(payload.get("answer") or "")

    text = redact_sensitive_text(text)
    for marker in REDACTION_MARKERS:
        text = text.replace(marker, "")
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ,.-")
    return text[:180].strip()


def _is_generic_reply(value: str) -> bool:
    normalized = re.sub(r"[^\w\u0590-\u05ff]+", "", value.lower())
    return normalized in _GENERIC_REPLIES


def _contact_name_parts(owner: str) -> tuple[str, str]:
    first_name, separator, title = owner.partition(" - ")
    return first_name.strip(), title.strip() if separator else "נציגת שירות"


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)
