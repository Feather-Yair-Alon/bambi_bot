from __future__ import annotations

from typing import Any


PRICE_SOURCE = "PaymentBtnsRows.Price"


def is_course_knowledge_tool(tool_id: str) -> bool:
    return tool_id.startswith("course_")


def safe_current_price_payload(payload: dict[str, Any]) -> dict[str, Any]:
    prices = []
    for item in payload.get("prices") or []:
        prices.append(
            {
                "price": item.get("price"),
                "name": item.get("name"),
                "title": item.get("title"),
                "product_name": item.get("product_name"),
                "catalog_number": item.get("catalog_number"),
                "description_for_bot": item.get("description_for_bot"),
                "payment_link_available": item.get("payment_link_available"),
                "payment_guidance": item.get("payment_guidance"),
            }
        )

    found = bool(payload.get("found") and prices)
    return {
        "found": found,
        "requires_user_choice": bool(payload.get("requires_user_choice")),
        "requires_representative": bool(payload.get("requires_representative")),
        "prices": prices,
        "price_source": payload.get("price_source") or PRICE_SOURCE,
        "reason": None if found else payload.get("reason") or "No current payment price was found.",
        "instruction": (
            "Use only these current prices when answering about price. "
            "Ignore any prices that appear inside the course content. "
            "Prices from this payload are excluding VAT unless a specific price explicitly says it includes VAT."
        ),
        "vat_note": "Prices are excluding VAT unless explicitly marked as VAT included.",
    }


def build_price_content_section(price_payload: dict[str, Any]) -> str:
    if not price_payload.get("found"):
        return (
            "\n\n## מחיר עדכני ממערכת התשלומים\n"
            "לא נמצא מחיר עדכני מאושר במערכת התשלומים. "
            "אין לציין מחיר מתוך גוף תוכן הקורס, גם אם מופיע שם מחיר. "
            "אם המשתמש שואל על מחיר, יש לומר שאין כרגע מחיר עדכני מאושר ולהעביר לנציג.\n"
        )

    prices = price_payload.get("prices") or []
    lines = [
        "\n\n## מחיר עדכני ממערכת התשלומים",
        f"מקור המחיר: {price_payload.get('price_source') or PRICE_SOURCE}.",
        "חובה להשתמש רק במחירים בסעיף זה ולהתעלם ממחירים שמופיעים בגוף תוכן הקורס אם הם שונים.",
    ]

    lines.append(
        'VAT note: prices in this section are excluding VAT unless explicitly marked as VAT included. '
        'When answering in Hebrew, say "לא כולל מע"מ".'
    )

    if len(prices) == 1:
        lines.append(f"המחיר המעודכן הוא {_format_price(prices[0].get('price'))}.")
        if prices[0].get("payment_guidance"):
            lines.append(str(prices[0]["payment_guidance"]))
    else:
        lines.append("נמצאו כמה אפשרויות מחיר. אם אין התאמה חד-משמעית לפי שם/תיאור האפשרות, שאל שאלת הבהרה קצרה.")
        for index, item in enumerate(prices, start=1):
            label = item.get("description_for_bot") or item.get("name") or item.get("title") or item.get("product_name") or "אפשרות מחיר"
            lines.append(f"{index}. {label}: {_format_price(item.get('price'))}")
            if item.get("payment_guidance"):
                lines.append(str(item["payment_guidance"]))

    return "\n".join(lines) + "\n"


def _format_price(value: Any) -> str:
    if isinstance(value, int):
        return f"{value:,} ש\"ח"
    if isinstance(value, float):
        return f"{value:,.0f} ש\"ח" if value.is_integer() else f"{value:,.2f} ש\"ח"
    if value is None or value == "":
        return "לא צוין מחיר"
    return f"{value} ש\"ח"
