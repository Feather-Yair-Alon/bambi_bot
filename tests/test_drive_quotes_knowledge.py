from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from scripts.update_knowledge_from_drive_quotes import (
    DocumentKnowledgeDecision,
    DriveFileRecord,
    DriveInventory,
    LogicalDocument,
    ToolSynthesis,
    audit_candidate,
    build_candidate,
    extract_docx,
    forbidden_output_reasons,
    group_physical_sources,
    load_inventory,
    normalize_logical_title,
    sanitize_source_text,
    should_skip_title,
    suggested_tool_id,
    synthesize_tool,
)


def record(file_id: str, title: str, *, folder: str = "", modified: str = "2026-08-17T08:00:00.000Z") -> DriveFileRecord:
    suffix = Path(title).suffix.lower()
    mime_type = {
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }[suffix]
    return DriveFileRecord(
        id=file_id,
        title=title,
        mime_type=mime_type,
        modified_time=modified,
        folder_path=folder,
        url=f"https://drive.google.com/file/d/{file_id}/view",
    )


def test_load_inventory_rejects_old_folder(tmp_path: Path) -> None:
    path = tmp_path / "inventory.json"
    payload = {
        "root_folder_id": "root",
        "excluded_folder_ids": ["old"],
        "generated_at": "2026-08-22T00:00:00Z",
        "count": 1,
        "files": [record("1", "קורס.pdf", folder="בטיחות/ישן").model_dump()],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="ישן"):
        load_inventory(path)


def test_docx_and_pdf_versions_share_logical_key() -> None:
    docx = record("docx", "הצעת מחיר קורס מלגזה 2026.docx")
    pdf = record("pdf", "הצעת מחיר קורס מלגזה 2026.pdf")

    assert normalize_logical_title(docx.title) == "קורס מלגזה"
    assert group_physical_sources([docx, pdf]) == {"קורס מלגזה": [docx, pdf]}


def test_specific_title_hints_precede_course_family_hints() -> None:
    assert suggested_tool_id("הצעת מחיר הסמכת מלגזת אדם הולך.pdf") == "course_pedestrian_forklift"
    assert suggested_tool_id("הצעת מחיר ריענון מלגזת אדם הולך.pdf") == "course_pedestrian_forklift_refresher"
    assert suggested_tool_id("הצעת מחיר לריענון מדריכי מלגזה.docx") == "course_forklift_instructor_refresher"
    assert suggested_tool_id("הצעת מחיר קורס מלגזה באנגלית.docx") == "course_forklift"
    assert (
        suggested_tool_id("הצעת מחיר יום עיון לקציני בטיחות מערכות עזר מתקדמות לנהג.docx")
        == "course_driver_assistance_systems_safety_officers"
    )
    assert should_skip_title("טופס רישום והזמנת עבודה ימי עיון 2026.docx")
    assert should_skip_title("הצעת מחיר לסקר סיכונים מפעל נשר רמלה.docx")


def test_tachograph_content_overrides_generic_driver_assistance_title() -> None:
    assert (
        suggested_tool_id(
            "הצעת מחיר השתלמות יום עיון בנושא מערכות עזר.docx",
            "טכוגרף דיגיטלי, תפעול טכוגרף וניתוח מידע מהטכוגרף",
        )
        == "course_digital_tachograph_safety_officers"
    )


def test_sanitizer_keeps_static_course_data_and_removes_commercial_data() -> None:
    text = """
קורס עבודה בגובה
מתכונת הקורס: יום אחד בשעות 08:00-16:00
תנאי סף: גיל 18 ומעלה
עלות הקורס: 407 ₪ לא כולל מע\"מ
מועד פתיחה: 21.10.2026
מדיניות ביטולים: ביטול עד 72 שעות מראש ללא חיוב
טופס הזמנת עבודה לקורס
פרטי הלקוח: שם __________
כרטיס אשראי 1234
"""

    result = sanitize_source_text(text)

    assert "08:00-16:00" in result
    assert "גיל 18" in result
    assert "מדיניות ביטולים" in result
    assert "407" not in result
    assert "21.10.2026" not in result
    assert "כרטיס אשראי" not in result
    assert "פרטי הלקוח" not in result


def test_output_validator_rejects_dynamic_and_personal_data_but_allows_hours() -> None:
    assert forbidden_output_reasons("הקורס מתקיים בשעות 08:00-16:00") == []
    assert "currency_or_vat" in forbidden_output_reasons('המחיר 400 ש"ח')
    assert "calendar_date" in forbidden_output_reasons("הקורס נפתח ב-21.10.2026")
    assert "registration_form" in forbidden_output_reasons("הרשמה מתבצעת לאחר חתימה על טופס")
    assert "email" in forbidden_output_reasons("office@example.com")


def test_extract_docx_reads_paragraph_text(tmp_path: Path) -> None:
    path = tmp_path / "course.docx"
    xml = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>Course facts</w:t></w:r></w:p></w:body>
    </w:document>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)

    assert extract_docx(path) == "Course facts"


def test_candidate_replaces_matched_tool_and_preserves_unmatched(tmp_path: Path) -> None:
    tools_dir = tmp_path / "tools"
    generated = tools_dir / "generated"
    generated.mkdir(parents=True)
    (generated / "course_forklift.txt").write_text("old forklift", encoding="utf-8")
    (generated / "course_other.txt").write_text("keep me", encoding="utf-8")
    manifest = {
        "tools": [
            {
                "tool_id": "course_forklift",
                "display_name": "מלגזה",
                "description": "old",
                "file_name": "generated/course_forklift.txt",
            },
            {
                "tool_id": "course_other",
                "display_name": "אחר",
                "description": "keep",
                "file_name": "generated/course_other.txt",
            },
        ]
    }
    source = record("drive1", "הצעת מחיר קורס מלגזה 2026.docx")
    document = LogicalDocument("קורס מלגזה", source, tmp_path / "source.docx", "raw", "clean", ())
    decision = DocumentKnowledgeDecision(
        has_course_value=True,
        tool_id="course_forklift",
        display_name="קורס מלגזה",
        tool_description="מידע על קורס מלגזה",
        aliases=["מלגזה"],
        static_summary="תנאי סף",
        reason="match",
        confidence="high",
    )
    synthesis = ToolSynthesis(
        display_name="קורס מלגזה",
        tool_description="מידע על קורס מלגזה",
        aliases=["מלגזה"],
        content="## תנאי קבלה\n- גיל 18 ומעלה.",
        confidence="high",
    )
    inventory = DriveInventory(
        root_folder_id="root",
        excluded_folder_ids=["old"],
        generated_at="2026-08-22T00:00:00Z",
        count=1,
        files=[source],
    )
    candidate = tmp_path / "candidate"

    candidate_manifest, audit = build_candidate(
        tools_dir,
        candidate,
        manifest,
        {"course_forklift": [(document, decision)]},
        {"course_forklift": synthesis},
        inventory,
    )

    assert "גיל 18" in (candidate / "generated/course_forklift.txt").read_text(encoding="utf-8")
    assert (candidate / "generated/course_other.txt").read_text(encoding="utf-8") == "keep me"
    assert len(candidate_manifest["tools"]) == 2
    assert audit[0]["status"] == "replaced"
    assert audit_candidate(candidate, {"course_forklift"}) == []


def test_synthesis_uses_safe_description_when_only_description_is_filtered(monkeypatch, tmp_path: Path) -> None:
    source = record("drive1", "הצעת מחיר קורס בדיקה.docx")
    document = LogicalDocument("קורס בדיקה", source, tmp_path / "source.docx", "raw", "clean", ())
    decision = DocumentKnowledgeDecision(
        has_course_value=True,
        tool_id="course_test",
        display_name="קורס בדיקה",
        tool_description="תיאור",
        aliases=["בדיקה"],
        static_summary="תנאי קבלה: גיל 18 ומעלה.",
        reason="test",
        confidence="high",
    )

    monkeypatch.setattr(
        "scripts.update_knowledge_from_drive_quotes.call_structured_llm",
        lambda *args, **kwargs: ToolSynthesis(
            display_name="קורס בדיקה",
            tool_description="מידע על מחיר הקורס",
            aliases=["בדיקה"],
            content="## תנאי קבלה\n- גיל 18 ומעלה.",
            confidence="high",
        ),
    )

    result = synthesize_tool(
        object(),
        "course_test",
        [(document, decision)],
        model="test",
        reasoning_effort="low",
        timeout=1,
    )

    assert result.tool_description.startswith("מידע סטטי מאושר על קורס בדיקה")
    assert "מחיר" not in result.tool_description


def test_heavy_vehicle_tool_includes_practical_process_without_dynamic_data() -> None:
    content = Path("app/tools-knowleage/generated/course_heavy_vehicle.txt").read_text(encoding="utf-8")

    assert "מכון צבר" in content
    assert "20 שיעורי נהיגה מעשיים" in content
    assert "קריית מלאכי" in content
    assert "בית ספר אחר לנהיגה" in content
    assert "80% לפחות" in content
    assert "חובת נוכחות בכל השיעורים" not in content
    assert "3,933" not in content
    assert "5,500" not in content
    assert "06.10.26" not in content
