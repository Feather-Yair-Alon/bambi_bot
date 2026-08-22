from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree

import httpx
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field
from pypdf import PdfReader


DEFAULT_INVENTORY = Path("data/drive_quotes_raw/inventory.json")
DEFAULT_RAW_DIR = Path("data/drive_quotes_raw/files")
DEFAULT_WORK_DIR = Path("data/drive_quotes_build")
DEFAULT_TOOLS_DIR = Path("app/tools-knowleage")
GENERATED_DIR_NAME = "generated"
MANIFEST_NAME = "generated_tools_manifest.json"
CONTENT_MARKER = "## תוכן מסוכם לסוכן"
MAX_SOURCE_CHARS = 14_000
MAX_DOCUMENT_SUMMARY_CHARS = 3_500
MAX_TOOL_CONTENT_CHARS = 8_000
SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
SOURCE_PREFERENCE = {".docx": 3, ".pdf": 2, ".doc": 1}
TITLE_TOOL_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"רענון.*מדריכ.*עבודה בגובה"), "course_work_at_height_instructor_refresher"),
    (re.compile(r"מדריכ.*עבודה בגובה"), "course_work_at_height_instructor"),
    (re.compile(r"רענון.*מדריכ.*מלגזה"), "course_forklift_instructor_refresher"),
    (re.compile(r"מדריך.*מלגזה"), "course_forklift_instructor"),
    (re.compile(r"רענון.*מלגזת אדם הולך"), "course_pedestrian_forklift_refresher"),
    (re.compile(r"הסמכת.*מלגזת אדם הולך"), "course_pedestrian_forklift"),
    (re.compile(r"רענון.*אחראי שינוע.*חומס"), "course_hazmat_transport_manager_refresh"),
    (re.compile(r"אחראי שינוע.*חומס"), "course_hazmat_transport_manager"),
    (re.compile(r"רענון.*חומס"), "course_hazardous_materials_transport_refresher"),
    (re.compile(r"חומס"), "course_hazardous_materials_transport"),
    (re.compile(r"רענון.*מלגזה"), "course_annual_forklift_refresher"),
    (re.compile(r"מלגזה"), "course_forklift"),
    (re.compile(r"רענון.*מפעילי עגורן"), "course_crane_operator_refresher"),
    (re.compile(r"חידוש רישיון עגורן"), "course_crane_certificate_renewal"),
    (re.compile(r"יום בחינות.*עגורן"), "course_crane_exam_day"),
    (re.compile(r"עגורן העמסה עצמית"), "course_self_loading_crane"),
    (re.compile(r"עגורן גשר"), "course_bridge_crane"),
    (re.compile(r"מתן איתות"), "course_signalman"),
    (re.compile(r"מערכות עזר(?: מתקדמות)?(?: לנהג)?"), "course_driver_assistance_systems_safety_officers"),
    (re.compile(r"טכוגרף"), "course_digital_tachograph_safety_officers"),
    (re.compile(r"חקר תאונות דרכים"), "course_traffic_accident_investigation_safety_officers"),
    (re.compile(r"אבטחת מטענים"), "course_cargo_securing"),
    (re.compile(r"רכב ציבורי"), "course_public_transport_vehicle"),
    (re.compile(r"רישיון מוביל קצר"), "course_transport_operator_license"),
    (re.compile(r"משא כבד"), "course_heavy_vehicle"),
    (re.compile(r"טרקטור"), "course_tractor"),
    (re.compile(r"מכונה ניידת"), "course_mobile_machine"),
    (re.compile(r"נאמני בטיחות"), "course_safety_trustees"),
    (re.compile(r"הדרכה טובה"), "course_good_instruction"),
    (re.compile(r"הדרכת בטיחות כללית"), "course_workplace_safety_training"),
    (re.compile(r"רענון נהגים שנתי"), "course_annual_driver_refresher"),
    (re.compile(r"הדרכת נהגים מקצועית"), "course_professional_driver_training"),
    (re.compile(r"הכשרה של חומרי נפץ"), "course_explosives_training"),
    (re.compile(r"גובה"), "course_work_at_height"),
)
TITLE_SKIP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"טופס רישום.*ימי עיון"),
    re.compile(r"סקר סיכונים.*מפעל"),
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


class DriveFileRecord(BaseModel):
    id: str
    title: str
    mime_type: str
    modified_time: str | None = None
    size: str | None = None
    folder_path: str = ""
    url: str | None = None


class DriveInventory(BaseModel):
    root_folder_id: str
    excluded_folder_ids: list[str] = Field(default_factory=list)
    generated_at: str
    count: int
    files: list[DriveFileRecord]


class DocumentKnowledgeDecision(BaseModel):
    has_course_value: bool
    tool_id: str | None = Field(description="Existing tool id, or a new snake_case id beginning with course_.")
    display_name: str | None
    tool_description: str | None
    aliases: list[str] = Field(default_factory=list)
    static_summary: str = Field(description="Hebrew static course facts only, without dates, prices, payment or PII.")
    reason: str
    confidence: Literal["low", "medium", "high"]


class ToolSynthesis(BaseModel):
    display_name: str
    tool_description: str
    aliases: list[str] = Field(default_factory=list)
    content: str = Field(description="Final Hebrew Markdown knowledge body without a top-level generated-file header.")
    confidence: Literal["medium", "high"]


@dataclass(frozen=True)
class LogicalDocument:
    logical_key: str
    source: DriveFileRecord
    source_path: Path
    extracted_text: str
    sanitized_text: str
    duplicate_ids: tuple[str, ...]


class NonRetryableLLMError(RuntimeError):
    pass


def load_inventory(path: Path) -> DriveInventory:
    inventory = DriveInventory.model_validate_json(path.read_text(encoding="utf-8"))
    if inventory.count != len(inventory.files):
        raise ValueError(f"Inventory count mismatch: declared={inventory.count}, actual={len(inventory.files)}")
    forbidden = [item for item in inventory.files if "ישן" in Path(item.folder_path).parts]
    if forbidden:
        titles = ", ".join(item.title for item in forbidden[:3])
        raise ValueError(f"Inventory contains files from an excluded 'ישן' folder: {titles}")
    return inventory


def safe_suffix(record: DriveFileRecord) -> str:
    suffix = Path(record.title).suffix.lower()
    if suffix in SOURCE_PREFERENCE:
        return suffix
    if record.mime_type == "application/pdf":
        return ".pdf"
    if record.mime_type == "application/msword":
        return ".doc"
    if record.mime_type.endswith("wordprocessingml.document"):
        return ".docx"
    return suffix or ".bin"


def raw_file_path(raw_dir: Path, record: DriveFileRecord) -> Path:
    return raw_dir / f"{record.id}{safe_suffix(record)}"


def download_url(file_id: str) -> str:
    return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"


def download_inventory_files(
    inventory: DriveInventory,
    raw_dir: Path,
    *,
    delay_seconds: float = 0.15,
    timeout_seconds: float = 60.0,
) -> dict[str, str]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    failures: dict[str, str] = {}
    with httpx.Client(follow_redirects=True, timeout=timeout_seconds, headers={"User-Agent": "BambiKnowledgeMaintenance/1.0"}) as client:
        for index, record in enumerate(inventory.files, start=1):
            path = raw_file_path(raw_dir, record)
            if path.exists() and path.stat().st_size > 0:
                continue
            try:
                response = client.get(download_url(record.id))
                response.raise_for_status()
                path.write_bytes(response.content)
                print(f"Downloaded {index}/{len(inventory.files)}: {record.folder_path}/{record.title}", flush=True)
            except Exception as exc:  # noqa: BLE001 - the report must retain per-file failures.
                failures[record.id] = f"{type(exc).__name__}: {exc}"
            if delay_seconds:
                time.sleep(delay_seconds)
    return failures


def normalize_logical_title(title: str) -> str:
    value = Path(title).stem.lower().replace("־", "-")
    value = value.replace("ריענון", "רענון")
    value = re.sub(r"\b(?:202[0-9]|20[0-9]{2})\b", " ", value)
    value = re.sub(r"(?:הצעת\s*מחיר|פורמט|הזמנת\s*עבודה|טופס\s*רישום)", " ", value)
    value = re.sub(r"(?:מכללת\s*במבי|במכללה)", " ", value)
    value = re.sub(r"\bpdf\b", " ", value)
    value = re.sub(r'''[-+_–—()\[\]{}'"]+''', " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def group_physical_sources(records: list[DriveFileRecord]) -> dict[str, list[DriveFileRecord]]:
    groups: dict[str, list[DriveFileRecord]] = {}
    for record in records:
        if record.mime_type not in SUPPORTED_MIME_TYPES:
            continue
        groups.setdefault(normalize_logical_title(record.title), []).append(record)
    return groups


def _modified_day(record: DriveFileRecord) -> str:
    return (record.modified_time or "")[:10]


def order_source_candidates(records: list[DriveFileRecord]) -> list[DriveFileRecord]:
    latest_day = max((_modified_day(item) for item in records), default="")
    newest = [item for item in records if _modified_day(item) == latest_day]
    older = [item for item in records if _modified_day(item) != latest_day]
    newest.sort(key=lambda item: (SOURCE_PREFERENCE.get(safe_suffix(item), 0), item.modified_time or ""), reverse=True)
    older.sort(key=lambda item: (item.modified_time or "", SOURCE_PREFERENCE.get(safe_suffix(item), 0)), reverse=True)
    return newest + older


def extract_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    lines: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t")).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def extract_pdf(path: Path) -> str:
    reader = PdfReader(path)
    return "\n".join((page.extract_text() or "").strip() for page in reader.pages if (page.extract_text() or "").strip())


def extract_file_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return extract_docx(path)
    if suffix == ".pdf":
        return extract_pdf(path)
    return ""


STOP_SECTION_PATTERNS = (
    re.compile(r"^טופס\s+(?:הזמנת\s+עבודה|רישום)", re.IGNORECASE),
    re.compile(r"^פרטי\s+הלקוח", re.IGNORECASE),
    re.compile(r"^אני\s+החתום\s+מטה", re.IGNORECASE),
    re.compile(r"^אבקשכם\s+לחייב", re.IGNORECASE),
)
FORBIDDEN_LINE_PATTERNS = (
    re.compile(r"(?:₪|ש[\"״']?ח|שקלים|מע[\"״']?מ)"),
    re.compile(r"(?:עלות|מחיר|תנאי\s+תשלום|אופן\s+התשלום|העברה\s+בנקאית|כרטיס\s+אשראי|חשבון\s+בנק|שוטף)", re.IGNORECASE),
    re.compile(r"(?:ההצעה\s+תקפה|תוקף\s+ההצעה)", re.IGNORECASE),
    re.compile(r"(?:מספר\s+כרטיס|שלוש\s+ספרות|שם\s+בעל\s+הכרטיס|פרטי\s+הלקוח)", re.IGNORECASE),
    re.compile(r"(?:הרשמה.*(?:טופס|חתימה)|חתימה.*טופס\s+הזמנת\s+עבודה)", re.IGNORECASE),
    re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
    re.compile(r"https?://|www\.", re.IGNORECASE),
    re.compile(r"\b0\d{1,2}[-\s]?\d{3}[-\s]?\d{4}\b"),
    re.compile(r"_{3,}"),
)
CALENDAR_DATE_PATTERN = re.compile(r"(?<!\d)\d{1,2}[./-]\d{1,2}[./-](?:\d{2}|\d{4})(?!\d)")
MONTH_YEAR_PATTERN = re.compile(
    r"^(?:ינואר|פברואר|מרץ|אפריל|מאי|יוני|יולי|אוגוסט|ספטמבר|אוקטובר|נובמבר|דצמבר)\s+20\d{2}$"
)


def sanitize_source_text(text: str) -> str:
    cleaned: list[str] = []
    for raw_line in text.replace("\r", "\n").splitlines():
        line = re.sub(r"[ \t]+", " ", raw_line).strip(" \t•✓❖")
        if not line:
            continue
        if any(pattern.search(line) for pattern in STOP_SECTION_PATTERNS):
            break
        if CALENDAR_DATE_PATTERN.search(line) or MONTH_YEAR_PATTERN.fullmatch(line):
            continue
        if any(pattern.search(line) for pattern in FORBIDDEN_LINE_PATTERNS):
            continue
        cleaned.append(line)
    value = "\n".join(cleaned)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    return value[:MAX_SOURCE_CHARS]


FORBIDDEN_OUTPUT_PATTERNS = (
    re.compile(r"(?:₪|ש[\"״']?ח|שקלים|מע[\"״']?מ)"),
    CALENDAR_DATE_PATTERN,
    re.compile(r"(?:מחיר|עלות\s+הקורס|תשלום|אשראי|בנק|חשבון\s+526835)", re.IGNORECASE),
    re.compile(r"(?:הרשמה.*(?:טופס|חתימה)|טופס\s+הזמנת\s+עבודה)", re.IGNORECASE),
    re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\b0\d{1,2}[-\s]?\d{3}[-\s]?\d{4}\b"),
    re.compile(r"https?://|www\.", re.IGNORECASE),
)


def forbidden_output_reasons(text: str) -> list[str]:
    reasons: list[str] = []
    labels = ["currency_or_vat", "calendar_date", "payment_or_price", "registration_form", "email", "phone", "url"]
    for label, pattern in zip(labels, FORBIDDEN_OUTPUT_PATTERNS, strict=True):
        if pattern.search(text):
            reasons.append(label)
    return reasons


def sanitize_generated_markdown(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if forbidden_output_reasons(line):
            continue
        lines.append(line.rstrip())
    value = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def build_logical_documents(
    inventory: DriveInventory,
    raw_dir: Path,
) -> tuple[list[LogicalDocument], list[dict[str, Any]]]:
    documents: list[LogicalDocument] = []
    skipped: list[dict[str, Any]] = []
    for logical_key, records in sorted(group_physical_sources(inventory.files).items()):
        selected: DriveFileRecord | None = None
        extracted = ""
        selected_path: Path | None = None
        failures: list[str] = []
        for candidate in order_source_candidates(records):
            path = raw_file_path(raw_dir, candidate)
            if not path.exists():
                failures.append(f"{candidate.id}: not downloaded")
                continue
            try:
                candidate_text = extract_file_text(path)
            except Exception as exc:  # noqa: BLE001 - extraction fallback is intentional.
                failures.append(f"{candidate.id}: {type(exc).__name__}: {exc}")
                continue
            if candidate_text.strip():
                selected = candidate
                selected_path = path
                extracted = candidate_text
                break
            failures.append(f"{candidate.id}: no extractable text")
        if selected is None or selected_path is None:
            skipped.append({"logical_key": logical_key, "titles": [item.title for item in records], "reason": "; ".join(failures)})
            continue
        sanitized = sanitize_source_text(extracted)
        if len(sanitized) < 80:
            skipped.append({"logical_key": logical_key, "titles": [item.title for item in records], "reason": "sanitized text is empty or too short"})
            continue
        documents.append(
            LogicalDocument(
                logical_key=logical_key,
                source=selected,
                source_path=selected_path,
                extracted_text=extracted,
                sanitized_text=sanitized,
                duplicate_ids=tuple(item.id for item in records if item.id != selected.id),
            )
        )
    return documents, skipped


def normalize_tool_id(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9_]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value[:64]


def suggested_tool_id(title: str, content: str = "") -> str | None:
    normalized = title.replace("ריענון", "רענון").replace('חומ"ס', "חומס").replace("חומ״ס", "חומס")
    if "מערכות עזר" in normalized and len(re.findall(r"טכוגרף", content)) >= 2:
        return "course_digital_tachograph_safety_officers"
    for pattern, tool_id in TITLE_TOOL_HINTS:
        if pattern.search(normalized):
            return tool_id
    return None


def should_skip_title(title: str) -> bool:
    normalized = title.replace("ריענון", "רענון")
    return any(pattern.search(normalized) for pattern in TITLE_SKIP_PATTERNS)


def load_existing_manifest(tools_dir: Path) -> dict[str, Any]:
    return json.loads((tools_dir / MANIFEST_NAME).read_text(encoding="utf-8"))


def tool_catalog_for_prompt(manifest: dict[str, Any]) -> str:
    return "\n".join(
        f"- {item['tool_id']}: {item.get('display_name', '')} | {item.get('description', '')}"
        for item in manifest.get("tools", [])
    )


def call_structured_llm(
    client: OpenAI,
    *,
    model: str,
    reasoning_effort: str,
    timeout: float,
    instructions: str,
    prompt: str,
    output_type: type[BaseModel],
    max_output_tokens: int,
) -> BaseModel:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = client.responses.parse(
                model=model,
                instructions=instructions,
                input=prompt,
                text_format=output_type,
                reasoning={"effort": reasoning_effort},
                max_output_tokens=max_output_tokens,
                timeout=timeout,
            )
            if response.output_parsed is None:
                raise RuntimeError("OpenAI returned no parsed output")
            return response.output_parsed
        except Exception as exc:  # noqa: BLE001 - batch retry is required.
            last_error = exc
            if "insufficient_quota" in str(exc):
                raise NonRetryableLLMError(f"LLM quota is exhausted: {exc}") from exc
            print(f"LLM call failed on attempt {attempt}/3: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(3 * attempt)
    raise RuntimeError(f"LLM call failed after retries: {last_error}") from last_error


def classify_document(
    client: OpenAI,
    document: LogicalDocument,
    manifest: dict[str, Any],
    *,
    model: str,
    reasoning_effort: str,
    timeout: float,
) -> DocumentKnowledgeDecision:
    tool_hint = suggested_tool_id(document.source.title, document.sanitized_text)
    prompt = f"""
אתה מעדכן knowledge tools של צ'אטבוט מכללת במבי ממסמכי הצעות מחיר מאושרים.

כללים מחייבים:
1. חלץ רק מידע סטטי על הקורס: קהל יעד, תנאי קבלה, מסמכים, מבנה, משך, שעות קבועות, שפות, תוכן, מבחנים, תעודה, ציוד, מיקום ומדיניות ביטולים.
2. אין להחזיר מחיר, סכום, מע\"מ, אגרה כספית, תאריך קורס, זמינות, לינק, טלפון, מייל, בנק, אשראי או פרטי לקוח.
3. הצעה לחברה או לאדם מסוים אינה הופכת תנאי ייחודי לאותו לקוח לעובדה כללית.
4. בחר כלי קיים כאשר הוא מתאים בדיוק. אם זה קורס אמיתי ללא כלי, צור tool_id באנגלית שמתחיל ב-course_.
5. אל תאחד קורסים דומים אך שונים, למשל קורס מול רענון או אחראי שינוע מול נהג חומ\"ס.
6. static_summary יהיה Markdown תמציתי בעברית עד {MAX_DOCUMENT_SUMMARY_CHARS} תווים.
7. אם זה טופס כללי, שירות שאינו קורס או מסמך ללא ידע שימושי, החזר has_course_value=false.
8. מיפוי חד-משמעי לפי שם המסמך: {tool_hint or 'אין; בחר לפי התוכן'}. אם צוין tool_id כאן והמסמך אכן עוסק בקורס, חובה להשתמש בו.

כלים קיימים:
{tool_catalog_for_prompt(manifest)}

מקור:
כותרת: {document.source.title}
תיקייה: {document.source.folder_path or 'root'}
עודכן: {document.source.modified_time}

טקסט מסונן:
{document.sanitized_text}
""".strip()
    decision = call_structured_llm(
        client,
        model=model,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
        instructions="Return only the structured result. Never include dynamic commercial data or personal information.",
        prompt=prompt,
        output_type=DocumentKnowledgeDecision,
        max_output_tokens=3_000,
    )
    assert isinstance(decision, DocumentKnowledgeDecision)
    if not decision.has_course_value:
        return decision
    tool_id = normalize_tool_id(tool_hint or decision.tool_id or "")
    existing_ids = {str(item["tool_id"]) for item in manifest.get("tools", [])}
    if not tool_id or (tool_id not in existing_ids and not tool_id.startswith("course_")):
        raise ValueError(f"Invalid tool id from LLM for {document.source.title}: {decision.tool_id}")
    decision.tool_id = tool_id
    decision.static_summary = sanitize_generated_markdown(decision.static_summary)[:MAX_DOCUMENT_SUMMARY_CHARS]
    if not decision.static_summary:
        raise ValueError(f"LLM returned no safe static facts for {document.source.title}")
    reasons = forbidden_output_reasons(decision.static_summary)
    if reasons:
        raise ValueError(f"Unsafe LLM summary for {document.source.title}: {', '.join(reasons)}")
    return decision


def synthesize_tool(
    client: OpenAI,
    tool_id: str,
    items: list[tuple[LogicalDocument, DocumentKnowledgeDecision]],
    *,
    model: str,
    reasoning_effort: str,
    timeout: float,
) -> ToolSynthesis:
    sources = []
    for document, decision in sorted(items, key=lambda item: item[0].source.modified_time or ""):
        sources.append(
            f"## {document.source.title}\nupdated_at: {document.source.modified_time}\n"
            f"aliases: {', '.join(decision.aliases)}\n{decision.static_summary}"
        )
    prompt = f"""
צור מסמך ידע סופי לכלי {tool_id} על בסיס הסיכומים הבאים בלבד.

כללים:
1. מסמך חדש יותר גובר במקרה של סתירה.
2. שמור הבדלים בין מסלולים, שפות, קורס ראשוני ורענון; אל תערבב קורסים שונים.
3. הסר כפילויות ומידע ייחודי ללקוח מסוים.
4. אין לכלול מחיר, סכום, מע\"מ, אגרה כספית, תאריך קורס, זמינות, לינק, טלפון, מייל, בנק, אשראי או פרטי לקוח.
5. מותר לכלול שעות פעילות קבועות, משך בשעות/ימים, אחוזי נוכחות ומדיניות ביטולים ללא סכומים כספיים.
6. content יהיה Markdown בעברית, ברור לסוכן שירות, ללא כותרת הקובץ האוטומטית וללא רשימת מקורות.
7. tool_description יסביר במדויק מתי להשתמש בכלי ולא יזכיר מחיר או מועד.
8. content יהיה עד {MAX_TOOL_CONTENT_CHARS} תווים.

{chr(10).join(sources)}
""".strip()
    synthesis = call_structured_llm(
        client,
        model=model,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
        instructions="Return only the structured final tool. Keep it factual, concise, Hebrew-first and free of dynamic commercial data.",
        prompt=prompt,
        output_type=ToolSynthesis,
        max_output_tokens=5_000,
    )
    assert isinstance(synthesis, ToolSynthesis)
    synthesis.content = sanitize_generated_markdown(synthesis.content)[:MAX_TOOL_CONTENT_CHARS]
    synthesis.tool_description = sanitize_generated_markdown(synthesis.tool_description)
    if not synthesis.content:
        raise ValueError(f"LLM returned no safe content for {tool_id}")
    if not synthesis.tool_description:
        fallback_name = next(
            (decision.display_name for _, decision in items if decision.display_name and re.search(r"[א-ת]", decision.display_name)),
            synthesis.display_name or tool_id,
        )
        synthesis.tool_description = (
            f"מידע סטטי מאושר על {fallback_name}, כולל תנאי קבלה, "
            "מבנה, משך, תכנים, מבחנים ותעודה."
        )
    reasons = forbidden_output_reasons(synthesis.content + "\n" + synthesis.tool_description)
    if reasons:
        raise ValueError(f"Unsafe synthesized tool {tool_id}: {', '.join(reasons)}")
    return synthesis


def read_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def generated_file_content(synthesis: ToolSynthesis, source_count: int) -> str:
    return (
        f"# {synthesis.display_name}\n\n"
        f"תיאור כלי: {synthesis.tool_description}\n\n"
        f"מספר מקורות: {source_count}\n\n"
        f"{CONTENT_MARKER}\n\n"
        f"{synthesis.content.strip()}\n"
    )


def build_candidate(
    tools_dir: Path,
    candidate_dir: Path,
    manifest: dict[str, Any],
    grouped: dict[str, list[tuple[LogicalDocument, DocumentKnowledgeDecision]]],
    syntheses: dict[str, ToolSynthesis],
    inventory: DriveInventory,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if candidate_dir.exists():
        shutil.rmtree(candidate_dir)
    candidate_dir.mkdir(parents=True)
    shutil.copytree(tools_dir / GENERATED_DIR_NAME, candidate_dir / GENERATED_DIR_NAME)

    tools_by_id = {str(item["tool_id"]): dict(item) for item in manifest.get("tools", [])}
    audit_tools: list[dict[str, Any]] = []
    for tool_id, synthesis in syntheses.items():
        sources = grouped[tool_id]
        source_records = [document.source for document, _ in sources]
        existing = tools_by_id.get(tool_id, {})
        file_name = str(existing.get("file_name") or f"{GENERATED_DIR_NAME}/{tool_id}.txt")
        output_path = candidate_dir / file_name
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(generated_file_content(synthesis, len(source_records)), encoding="utf-8")
        tools_by_id[tool_id] = {
            **existing,
            "tool_id": tool_id,
            "display_name": synthesis.display_name,
            "description": synthesis.tool_description,
            "file_name": file_name,
            "source_count": len(source_records),
            "source_files": [f"drive:{record.id}" for record in source_records],
            "aliases": synthesis.aliases,
            "source_type": "drive_quote",
            "source_metadata": [
                {
                    "file_id": record.id,
                    "title": record.title,
                    "folder_path": record.folder_path,
                    "modified_time": record.modified_time,
                    "url": record.url,
                }
                for record in source_records
            ],
        }
        audit_tools.append(
            {
                "tool_id": tool_id,
                "display_name": synthesis.display_name,
                "status": "replaced" if existing else "created",
                "source_count": len(source_records),
                "sources": [record.title for record in source_records],
            }
        )

    ordered_ids = [str(item["tool_id"]) for item in manifest.get("tools", [])]
    ordered_ids.extend(tool_id for tool_id in tools_by_id if tool_id not in ordered_ids)
    candidate_manifest = {
        **manifest,
        "generated_at": datetime.now(UTC).isoformat(),
        "generator": "scripts/update_knowledge_from_drive_quotes.py",
        "drive_run": {
            "root_folder_id": inventory.root_folder_id,
            "excluded_folder_ids": inventory.excluded_folder_ids,
            "inventory_generated_at": inventory.generated_at,
            "physical_file_count": inventory.count,
        },
        "tools": [tools_by_id[tool_id] for tool_id in ordered_ids],
    }
    write_json(candidate_dir / MANIFEST_NAME, candidate_manifest)
    return candidate_manifest, audit_tools


def apply_candidate(tools_dir: Path, candidate_dir: Path, backup_root: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = backup_root / f"drive_quotes_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    shutil.copytree(tools_dir / GENERATED_DIR_NAME, backup_dir / GENERATED_DIR_NAME)
    shutil.copy2(tools_dir / MANIFEST_NAME, backup_dir / MANIFEST_NAME)

    generated_dir = tools_dir / GENERATED_DIR_NAME
    shutil.rmtree(generated_dir)
    shutil.copytree(candidate_dir / GENERATED_DIR_NAME, generated_dir)
    shutil.copy2(candidate_dir / MANIFEST_NAME, tools_dir / MANIFEST_NAME)
    return backup_dir


def audit_candidate(candidate_dir: Path, updated_tool_ids: set[str]) -> list[dict[str, Any]]:
    manifest = json.loads((candidate_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    failures: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in manifest.get("tools", []):
        tool_id = str(item["tool_id"])
        if tool_id in seen:
            failures.append({"tool_id": tool_id, "reason": "duplicate tool id"})
        seen.add(tool_id)
        path = candidate_dir / str(item["file_name"])
        if not path.exists():
            failures.append({"tool_id": tool_id, "reason": "missing file"})
            continue
        if tool_id in updated_tool_ids:
            reasons = forbidden_output_reasons(path.read_text(encoding="utf-8"))
            if reasons:
                failures.append({"tool_id": tool_id, "reason": ", ".join(reasons)})
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description="One-time rebuild of Bambi course knowledge tools from approved Drive quotes.")
    parser.add_argument("--inventory", default=str(DEFAULT_INVENTORY))
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    parser.add_argument("--work-dir", default=str(DEFAULT_WORK_DIR))
    parser.add_argument("--tools-dir", default=str(DEFAULT_TOOLS_DIR))
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5.5"))
    parser.add_argument("--reasoning-effort", default=os.getenv("OPENAI_REASONING_EFFORT", "low"))
    parser.add_argument("--llm-timeout", type=float, default=180.0)
    parser.add_argument("--download-delay", type=float, default=0.15)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--max-documents", type=int, default=0)
    parser.add_argument(
        "--refresh-tool",
        action="append",
        default=[],
        help="Ignore cached decisions and synthesis for this tool ID. May be repeated.",
    )
    parser.add_argument("--prepare-only", action="store_true", help="Download, extract and report without calling the LLM.")
    parser.add_argument("--apply", action="store_true", help="Replace active generated tools after all audits pass.")
    args = parser.parse_args()

    load_dotenv()
    inventory_path = Path(args.inventory)
    raw_dir = Path(args.raw_dir)
    work_dir = Path(args.work_dir)
    tools_dir = Path(args.tools_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    inventory = load_inventory(inventory_path)
    if not args.skip_download:
        download_failures = download_inventory_files(inventory, raw_dir, delay_seconds=args.download_delay)
        write_json(work_dir / "download_failures.json", download_failures)

    documents, extraction_skips = build_logical_documents(inventory, raw_dir)
    if args.max_documents:
        documents = documents[: args.max_documents]
    print(f"Physical files: {inventory.count}; logical extractable documents: {len(documents)}", flush=True)
    write_json(
        work_dir / "extraction_report.json",
        {
            "physical_file_count": inventory.count,
            "logical_document_count": len(documents),
            "documents": [
                {
                    "logical_key": document.logical_key,
                    "file_id": document.source.id,
                    "title": document.source.title,
                    "folder_path": document.source.folder_path,
                    "modified_time": document.source.modified_time,
                    "duplicate_ids": document.duplicate_ids,
                    "sanitized_chars": len(document.sanitized_text),
                }
                for document in documents
            ],
            "skipped": extraction_skips,
        },
    )
    if args.prepare_only:
        print(f"Preparation complete. Report: {work_dir / 'extraction_report.json'}", flush=True)
        return

    manifest = load_existing_manifest(tools_dir)
    existing_tools_by_id = {str(item["tool_id"]): item for item in manifest.get("tools", [])}
    client = OpenAI()
    decision_path = work_dir / "document_decisions.json"
    decision_checkpoint = read_checkpoint(decision_path)
    decisions: dict[str, DocumentKnowledgeDecision] = {}
    errors: list[dict[str, Any]] = []
    for index, document in enumerate(documents, start=1):
        expected_hint = suggested_tool_id(document.source.title, document.sanitized_text)
        cached = decision_checkpoint.get(document.source.id)
        if cached and (
            str(cached.get("tool_id") or "") in args.refresh_tool
            or (expected_hint and str(cached.get("tool_id") or "") != expected_hint)
        ):
            cached = None
        if cached:
            decision = DocumentKnowledgeDecision.model_validate(cached)
        elif should_skip_title(document.source.title):
            decision = DocumentKnowledgeDecision(
                has_course_value=False,
                tool_id=None,
                display_name=None,
                tool_description=None,
                aliases=[],
                static_summary="",
                reason="Registration form or customer-specific service proposal, excluded by deterministic policy.",
                confidence="high",
            )
            decision_checkpoint[document.source.id] = decision.model_dump()
            write_json(decision_path, decision_checkpoint)
        else:
            try:
                decision = classify_document(
                    client,
                    document,
                    manifest,
                    model=args.model,
                    reasoning_effort=args.reasoning_effort,
                    timeout=args.llm_timeout,
                )
            except Exception as exc:  # noqa: BLE001 - one bad source must be reported without corrupting tools.
                errors.append({"file_id": document.source.id, "title": document.source.title, "stage": "classify", "error": f"{type(exc).__name__}: {exc}"})
                write_json(work_dir / "errors.json", errors)
                continue
            decision_checkpoint[document.source.id] = decision.model_dump()
            write_json(decision_path, decision_checkpoint)
        decisions[document.source.id] = decision
        print(f"Classified {index}/{len(documents)}: {document.source.title} -> {decision.tool_id or 'skip'}", flush=True)

    grouped: dict[str, list[tuple[LogicalDocument, DocumentKnowledgeDecision]]] = {}
    skipped_decisions: list[dict[str, Any]] = []
    for document in documents:
        decision = decisions.get(document.source.id)
        if not decision or not decision.has_course_value or not decision.tool_id or decision.confidence == "low":
            skipped_decisions.append(
                {
                    "file_id": document.source.id,
                    "title": document.source.title,
                    "reason": decision.reason if decision else "classification failed",
                    "confidence": decision.confidence if decision else None,
                }
            )
            continue
        grouped.setdefault(decision.tool_id, []).append((document, decision))

    synthesis_path = work_dir / "tool_syntheses.json"
    synthesis_checkpoint = read_checkpoint(synthesis_path)
    syntheses: dict[str, ToolSynthesis] = {}
    for index, (tool_id, items) in enumerate(sorted(grouped.items()), start=1):
        cached = None if tool_id in args.refresh_tool else synthesis_checkpoint.get(tool_id)
        if cached:
            synthesis = ToolSynthesis.model_validate(cached)
        else:
            try:
                synthesis = synthesize_tool(
                    client,
                    tool_id,
                    items,
                    model=args.model,
                    reasoning_effort=args.reasoning_effort,
                    timeout=args.llm_timeout,
                )
            except Exception as exc:  # noqa: BLE001 - do not replace a tool with failed synthesis.
                errors.append({"tool_id": tool_id, "stage": "synthesize", "error": f"{type(exc).__name__}: {exc}"})
                write_json(work_dir / "errors.json", errors)
                continue
            synthesis_checkpoint[tool_id] = synthesis.model_dump()
            write_json(synthesis_path, synthesis_checkpoint)
        existing_tool = existing_tools_by_id.get(tool_id)
        if existing_tool:
            synthesis.display_name = str(existing_tool.get("display_name") or synthesis.display_name)
        elif not re.search(r"[א-ת]", synthesis.display_name):
            synthesis.display_name = next(
                (decision.display_name for _, decision in items if decision.display_name and re.search(r"[א-ת]", decision.display_name)),
                tool_id,
            )
        synthesis.content = sanitize_generated_markdown(synthesis.content)[:MAX_TOOL_CONTENT_CHARS]
        synthesis.tool_description = sanitize_generated_markdown(synthesis.tool_description)
        synthesis.tool_description = synthesis.tool_description.replace(tool_id, synthesis.display_name)
        if not synthesis.tool_description:
            synthesis.tool_description = (
                f"מידע סטטי מאושר על {synthesis.display_name}, כולל תנאי קבלה, "
                "מבנה, משך, תכנים, מבחנים ותעודה."
            )
        cached_reasons = forbidden_output_reasons(synthesis.content + "\n" + synthesis.tool_description)
        if not synthesis.content or cached_reasons:
            errors.append(
                {
                    "tool_id": tool_id,
                    "stage": "validate_cached_synthesis",
                    "error": ", ".join(cached_reasons) or "empty safe synthesis",
                }
            )
            continue
        synthesis.aliases = sorted({alias.strip() for _, decision in items for alias in decision.aliases if alias.strip()} | set(synthesis.aliases))
        syntheses[tool_id] = synthesis
        print(f"Synthesized {index}/{len(grouped)}: {tool_id}", flush=True)

    candidate_dir = work_dir / "candidate"
    candidate_manifest, audit_tools = build_candidate(tools_dir, candidate_dir, manifest, grouped, syntheses, inventory)
    updated_ids = set(syntheses)
    candidate_failures = audit_candidate(candidate_dir, updated_ids)
    matched_existing = {str(item["tool_id"]) for item in manifest.get("tools", [])} & updated_ids
    unmatched_existing = [
        {"tool_id": item["tool_id"], "display_name": item.get("display_name"), "status": "preserved_unverified"}
        for item in manifest.get("tools", [])
        if str(item["tool_id"]) not in matched_existing
    ]
    audit = {
        "generated_at": datetime.now(UTC).isoformat(),
        "inventory_file_count": inventory.count,
        "logical_document_count": len(documents),
        "excluded_folder_ids": inventory.excluded_folder_ids,
        "updated_tool_count": len(updated_ids),
        "candidate_tool_count": len(candidate_manifest.get("tools", [])),
        "tools": audit_tools,
        "preserved_unverified_tools": unmatched_existing,
        "extraction_skips": extraction_skips,
        "decision_skips": skipped_decisions,
        "errors": errors,
        "candidate_failures": candidate_failures,
    }
    write_json(work_dir / "audit_report.json", audit)

    if errors or candidate_failures:
        raise SystemExit(f"Build did not pass audit: errors={len(errors)}, candidate_failures={len(candidate_failures)}")
    if not updated_ids:
        raise SystemExit("Build produced no updated tools; refusing to apply.")

    print(f"Candidate passed audit with {len(updated_ids)} updated tools.", flush=True)
    if args.apply:
        backup_dir = apply_candidate(tools_dir, candidate_dir, Path("data/knowledge_build_backups"))
        print(f"Applied candidate. Backup: {backup_dir}", flush=True)
    else:
        print("Dry run complete. Pass --apply to replace active tools.", flush=True)


if __name__ == "__main__":
    main()
