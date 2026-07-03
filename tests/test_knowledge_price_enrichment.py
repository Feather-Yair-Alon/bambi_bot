from app.services.knowledge_price_enrichment import (
    build_price_content_section,
    is_course_knowledge_tool,
    safe_current_price_payload,
)


def test_is_course_knowledge_tool_detects_generated_course_tools() -> None:
    assert is_course_knowledge_tool("course_forklift")
    assert not is_course_knowledge_tool("safety_courses")
    assert not is_course_knowledge_tool("contacting_bambi_college")


def test_safe_current_price_payload_removes_payment_button_ids() -> None:
    payload = safe_current_price_payload(
        {
            "found": True,
            "prices": [
                {
                    "price": 1000,
                    "payment_btn_id": "internal-id",
                    "name": "תשלום קורס",
                    "product_name": "קורס בדיקה",
                }
            ],
            "price_source": "PaymentBtnsRows.Price",
        }
    )

    assert payload["found"] is True
    assert payload["prices"][0]["price"] == 1000
    assert "payment_btn_id" not in payload["prices"][0]


def test_build_price_content_section_single_price_tells_agent_to_use_api_price() -> None:
    section = build_price_content_section(
        {
            "found": True,
            "price_source": "PaymentBtnsRows.Price",
            "prices": [{"price": 1000, "name": "תשלום קורס"}],
        }
    )

    assert "המחיר המעודכן הוא 1,000 ש\"ח" in section
    assert "חובה להשתמש רק במחירים בסעיף זה" in section
    assert "PaymentBtnsRows.Price" in section


def test_build_price_content_section_missing_price_blocks_old_content_prices() -> None:
    section = build_price_content_section({"found": False, "prices": [], "reason": "No current payment price was found."})

    assert "לא נמצא מחיר עדכני מאושר" in section
    assert "אין לציין מחיר מתוך גוף תוכן הקורס" in section
