import re
from pathlib import Path

from app.services.contact_channels import CONTACT_CHANNELS, OFFICE_CONTACT, ContactChannelService


def test_forklift_uses_dedicated_forklift_whatsapp() -> None:
    service = ContactChannelService()

    result = service.find_course_contact("קורס מלגזה")

    assert result["found"] is True
    assert result["contact"]["owner"] == "אליאור - רכזת מלגזה ורענוני מלגזה"
    assert result["contact"]["phone"] == "054-968-8028"


def test_forklift_instructor_uses_elior_forklift_contact() -> None:
    service = ContactChannelService()

    result = service.find_course_contact("ריענון מדריך מלגזה")

    assert result["found"] is True
    assert result["contact"]["phone"] == "054-968-8028"


def test_hazmat_and_public_transport_use_marina_transport_whatsapp() -> None:
    service = ContactChannelService()

    for course_name in ["ריענון חומס", "ריענון אחראי שינוע חומס", "קורס רכב ציבורי", "קורס הוראת נהיגה"]:
        result = service.find_course_contact(course_name)

        assert result["found"] is True, course_name
        assert result["contact"]["owner"] == "מרינה - מנהלת מחלקת תחבורה"
        assert result["contact"]["phone"] == "054-580-6131"


def test_cranes_use_hen_whatsapp() -> None:
    service = ContactChannelService()

    for course_name in ["קורס עגורן גשר", "חידוש רישיון מנוף", "קורס אתתים"]:
        result = service.find_course_contact(course_name)

        assert result["found"] is True, course_name
        assert result["contact"]["owner"] == "חן - רכזת תחום מנופים"
        assert result["contact"]["phone"] == "054-904-7872"


def test_work_at_height_uses_mika_whatsapp() -> None:
    service = ContactChannelService()

    for course_name in ["קורס עבודה בגובה", "רענון מדריכי עבודה בגובה", "קורס מדריך עבודה בגובה"]:
        result = service.find_course_contact(course_name)

        assert result["found"] is True, course_name
        assert result["contact"]["owner"] == "מיקה - רכזת תחום עבודה בגובה"
        assert result["contact"]["phone"] == "054-940-5419"


def test_safety_courses_use_tali_whatsapp() -> None:
    service = ContactChannelService()

    for course_name in ["השתלמות טכוגרף", "קורס נאמני בטיחות", "יום עיון לקציני בטיחות", "אבטחת מטענים"]:
        result = service.find_course_contact(course_name)

        assert result["found"] is True, course_name
        assert result["contact"]["owner"] == "טלי - מנהלת מחלקת בטיחות"
        assert result["contact"]["phone"] == "052-702-3884"


def test_tractor_mobile_machine_and_heavy_vehicle_use_yarin_whatsapp() -> None:
    service = ContactChannelService()

    for course_name in ["קורס טרקטור", "קורס מכונה ניידת", "קורס משא כבד"]:
        result = service.find_course_contact(course_name)

        assert result["found"] is True, course_name
        assert result["contact"]["owner"] == "ירין - רכזת טרקטורים, מכונה ניידת ומשא כבד"
        assert result["contact"]["phone"] == "054-904-7652"


def test_unknown_course_falls_back_to_office_contact() -> None:
    service = ContactChannelService()

    result = service.find_course_contact("קורס לא ידוע")

    assert result["found"] is False
    assert result["fallback"]["phone"] == "074-70-87-030"


def test_only_approved_bambi_phone_numbers_appear_in_bot_content() -> None:
    approved = {re.sub(r"\D", "", channel.phone) for channel in CONTACT_CHANNELS}
    approved.update({re.sub(r"\D", "", OFFICE_CONTACT["phone"]), "088693690"})
    phone_pattern = re.compile(r"(?<!\d)(?:0(?:5\d|7\d)(?:[- ]?\d){7}|0[23489](?:[- ]?\d){7})(?!\d)")
    app_root = Path(__file__).parents[1] / "app"
    unexpected: list[tuple[str, str]] = []

    for path in app_root.rglob("*"):
        if path.suffix not in {".py", ".txt", ".md", ".html"} or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in phone_pattern.findall(text):
            if re.sub(r"\D", "", match) not in approved:
                unexpected.append((str(path.relative_to(app_root)), match))

    assert unexpected == []
