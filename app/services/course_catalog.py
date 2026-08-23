from __future__ import annotations

import re
from typing import Any


# Specific aliases must precede broad course-family aliases.
COURSE_CATEGORY_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("80018", ("רענון מדריך עבודה בגובה", "ריענון מדריך עבודה בגובה", "רענון מדריכי עבודה בגובה", "ריענון מדריכי עבודה בגובה")),
    ("80017", ("מדריך עבודה בגובה", "מדריכי עבודה בגובה", "מדריך גובה")),
    ("80052", ("רענון אחראי שינוע חומס", "ריענון אחראי שינוע חומס")),
    ("80051", ("אחראי שינוע חומס",)),
    ("80003", ("רענון מלגזה", "ריענון מלגזה", "רענון שנתי למלגזה", "ריענון שנתי למלגזה")),
    ("80012", ("משאית משא כבד", "רכב משא כבד", "משא כבד", "משאית מעל 12 טון")),
    ("80031", ("נאמני בטיחות", "נאמן בטיחות")),
    ("80015", ("עבודה בגובה",)),
    ("80001", ("מלגזה",)),
)

KNOWLEDGE_TOOL_CATEGORY_CODES: dict[str, str] = {
    "course_work_at_height": "80015",
    "course_work_at_height_instructor": "80017",
    "course_work_at_height_instructor_refresher": "80018",
    "course_forklift": "80001",
    "course_annual_forklift_refresher": "80003",
    "course_safety_trustees": "80031",
    "course_hazmat_transport_manager_refresh": "80052",
    "course_hazmat_transport_manager": "80051",
    "course_heavy_vehicle": "80012",
}

LANGUAGE_ALIASES: dict[str, tuple[str, ...]] = {
    "עברית": ("עברית",),
    "רוסית": ("רוסית", "רוסי"),
    "אנגלית": ("אנגלית", "אנגלי"),
    "תאית": ("תאית", "תאילנדית", "תאילנדים", "תאילנדי"),
}


def normalize_catalog_text(value: Any) -> str:
    text = str(value or "").lower().replace("ריענון", "רענון")
    text = text.replace('חומ"ס', "חומס").replace("חומ״ס", "חומס")
    text = re.sub(r'["\'״׳`´]+', "", text)
    return re.sub(r"\s+", " ", text).strip()


def resolve_course_category_code(value: Any) -> str | None:
    query = normalize_catalog_text(value)
    for code, aliases in COURSE_CATEGORY_ALIASES:
        if any(normalize_catalog_text(alias) in query for alias in aliases):
            return code
    return None


def detect_course_language(value: Any) -> str | None:
    query = normalize_catalog_text(value)
    for language, aliases in LANGUAGE_ALIASES.items():
        if any(alias in query for alias in aliases):
            return language
    return None


def text_matches_language(value: Any, language: str) -> bool:
    text = normalize_catalog_text(value)
    return any(alias in text for alias in LANGUAGE_ALIASES[language])
