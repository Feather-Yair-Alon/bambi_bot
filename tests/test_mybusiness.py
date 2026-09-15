import asyncio

from app.config import Settings
from app.services.mybusiness import (
    EXTERNAL_COURSE_NO_ID,
    EXTERNAL_COURSE_YES_ID,
    future_course_start_date_filter,
    course_row_matches_search,
    course_search_keywords,
    is_external_course,
    map_available_course,
    match_categories,
    normalize_course_search,
    normalize_identifier_variants,
    pointer,
    validate_course_for_registration,
    with_internal_courses_only_filter,
    MyBusinessService,
)


def test_future_course_start_date_filter_requires_course_not_started() -> None:
    now = "2026-07-01T12:00:00.000Z"

    assert future_course_start_date_filter(now) == {"$gt": {"__type": "Date", "iso": now}}


def test_with_internal_courses_only_filter_allows_missing_or_no_external_flag() -> None:
    where = {"ProductCategory": pointer("ProductCategories", "cat1")}

    assert with_internal_courses_only_filter(where) == {
        "$and": [
            where,
            {
                "$or": [
                    {"ExternalCourse": {"$exists": False}},
                    {"ExternalCourse": None},
                    {"ExternalCourse": pointer("C_YesNoList", EXTERNAL_COURSE_NO_ID)},
                ]
            },
        ]
    }


def test_is_external_course_detects_yes_pointer_or_label() -> None:
    assert is_external_course({"ExternalCourse": pointer("C_YesNoList", EXTERNAL_COURSE_YES_ID)})
    assert is_external_course({"ExternalCourse": {"objectId": "other", "Name": "כן"}})
    assert not is_external_course({"ExternalCourse": pointer("C_YesNoList", EXTERNAL_COURSE_NO_ID)})
    assert not is_external_course({})


def test_normalize_identifier_variants_for_israeli_mobile() -> None:
    assert normalize_identifier_variants("050-123-4567") == [
        "050-123-4567",
        "0501234567",
        "501234567",
        "972501234567",
        "+972501234567",
    ]


def test_normalize_course_search_strips_common_prefixes() -> None:
    assert normalize_course_search("קורס מלגזה") == "מלגזה"
    assert normalize_course_search("רישיון מכונה ניידת") == "מכונה ניידת"


def test_course_keyword_matching_finds_long_category_from_partial_course_name() -> None:
    tachograph_course = "\u05d4\u05e9\u05ea\u05dc\u05de\u05d5\u05ea \u05d8\u05db\u05d5\u05d2\u05e8\u05e3 \u05d3\u05d9\u05d2\u05d9\u05d8\u05dc\u05d9 \u05dc\u05e7\u05e6\u05d9\u05e0\u05d9 \u05d1\u05d8\u05d9\u05d7\u05d5\u05ea"
    safety_officer_course = "\u05e7\u05d5\u05e8\u05e1 \u05e7\u05e6\u05d9\u05e0\u05d9 \u05d1\u05d8\u05d9\u05d7\u05d5\u05ea \u05d1\u05ea\u05e2\u05d1\u05d5\u05e8\u05d4"
    query = "\u05d8\u05db\u05d5\u05d2\u05e8\u05e3 \u05d3\u05d9\u05d2\u05d9\u05d8\u05dc\u05d9"
    categories = [
        {"category_id": "cat1", "name": tachograph_course, "code": ""},
        {"category_id": "cat2", "name": safety_officer_course, "code": ""},
    ]

    assert course_search_keywords(query) == ["\u05d8\u05db\u05d5\u05d2\u05e8\u05e3", "\u05d3\u05d9\u05d2\u05d9\u05d8\u05dc\u05d9"]
    assert match_categories(categories, query) == [categories[0]]


def test_course_keyword_matching_does_not_match_single_weak_keyword() -> None:
    tachograph_course = "\u05d4\u05e9\u05ea\u05dc\u05de\u05d5\u05ea \u05d8\u05db\u05d5\u05d2\u05e8\u05e3 \u05d3\u05d9\u05d2\u05d9\u05d8\u05dc\u05d9 \u05dc\u05e7\u05e6\u05d9\u05e0\u05d9 \u05d1\u05d8\u05d9\u05d7\u05d5\u05ea"
    query = "\u05e7\u05e6\u05d9\u05e0\u05d9"
    categories = [{"category_id": "cat1", "name": tachograph_course, "code": ""}]

    assert match_categories(categories, query) == []


def test_course_row_matching_finds_tachograph_even_without_digital_in_course_name() -> None:
    query = "\u05d8\u05db\u05d5\u05d2\u05e8\u05e3 \u05d3\u05d9\u05d2\u05d9\u05d8\u05dc\u05d9"
    row = {
        "Name": "\u05d4\u05e9\u05ea\u05dc\u05de\u05d5\u05ea \u05d8\u05db\u05d5\u05d2\u05e8\u05e3 \u05dc\u05e7\u05e6\u05d9\u05e0\u05d9 \u05d1\u05d8\u05d9\u05d7\u05d5\u05ea 12.7.26",
        "ProductCategory": {"Name": "\u05d9\u05d5\u05dd \u05e2\u05d9\u05d5\u05df \u05dc\u05e7\u05e6\u05d1\u05ea", "Code": "80049"},
        "ProductId": {"Name": "\u05d9\u05d5\u05dd \u05e2\u05d9\u05d5\u05df \u05dc\u05e7\u05e6\u05d9\u05e0\u05d9 \u05d1\u05d8\u05d9\u05d7\u05d5\u05ea"},
    }

    assert course_row_matches_search(row, query) is True


def test_course_row_matching_does_not_mix_other_safety_officer_study_days() -> None:
    query = "\u05d8\u05db\u05d5\u05d2\u05e8\u05e3 \u05d3\u05d9\u05d2\u05d9\u05d8\u05dc\u05d9"
    row = {
        "Name": "8.9.26 \u05d9\u05d5\u05dd \u05e2\u05d9\u05d5\u05df \u05dc\u05e7\u05e6\u05d9\u05e0\u05d9 \u05d1\u05d8\u05d9\u05d7\u05d5\u05ea \u05d1\u05e0\u05d5\u05e9\u05d0 \u05de\u05e2\u05e8\u05db\u05d5\u05ea \u05e2\u05d6\u05e8 ADAS",
        "ProductCategory": {"Name": "\u05d9\u05d5\u05dd \u05e2\u05d9\u05d5\u05df \u05dc\u05e7\u05e6\u05d1\u05ea", "Code": "80049"},
        "ProductId": {"Name": "\u05d9\u05d5\u05dd \u05e2\u05d9\u05d5\u05df \u05dc\u05e7\u05e6\u05d9\u05e0\u05d9 \u05d1\u05d8\u05d9\u05d7\u05d5\u05ea"},
    }

    assert course_row_matches_search(row, query) is False


def test_safety_controller_search_requires_distinctive_controller_term() -> None:
    query = "קורס בקר בטיחות"
    unrelated_row = {
        "Name": "יום עיון בטיחות בבנייה ופיגומים",
        "ProductCategory": {"Name": "יום עיון", "Code": "80030"},
        "ProductId": {"Name": "יום עיון בטיחות"},
    }
    matching_row = {
        "Name": "הכשרת בקרי בטיחות בבנייה",
        "ProductCategory": {"Name": "הכשרות בטיחות", "Code": ""},
        "ProductId": {"Name": "קורס בקר בטיחות"},
    }

    assert course_search_keywords(query) == ["בקר"]
    assert course_row_matches_search(unrelated_row, query) is False
    assert course_row_matches_search(matching_row, query) is True


def test_map_available_course_skips_full_course() -> None:
    category = {"category_id": "cat1", "name": "מלגזה", "code": "80001"}
    row = {
        "objectId": "course1",
        "Name": "קורס מלגזה",
        "MaxCapacity": 10,
        "RegisteredStudents": 10,
    }

    assert map_available_course(row, category) is None


def test_map_available_course_skips_external_course() -> None:
    category = {"category_id": "cat1", "name": "×˜×¨×§×˜×•×¨", "code": "80007"}
    row = {
        "objectId": "course1",
        "Name": "×§×•×¨×¡ ×˜×¨×§×˜×•×¨ × ×¢×Ÿ",
        "MaxCapacity": 10,
        "RegisteredStudents": 1,
        "ExternalCourse": pointer("C_YesNoList", EXTERNAL_COURSE_YES_ID),
    }

    assert map_available_course(row, category) is None


def test_map_available_course_returns_positive_capacity() -> None:
    category = {"category_id": "cat1", "name": "מלגזה", "code": "80001"}
    row = {
        "objectId": "course1",
        "Name": "קורס מלגזה",
        "StartDate": {"__type": "Date", "iso": "2026-06-19T09:00:00.000Z"},
        "MaxCapacity": 10,
        "RegisteredStudents": 7,
        "StatusId": {"objectId": "status1", "Name": "פתוח לרישום"},
        "ProductId": {"objectId": "product1", "Name": "קורס מלגזה", "Price": 400},
    }

    course = map_available_course(row, category)

    assert course is not None
    assert course["available_seats"] == 3
    assert course["category_id"] == "cat1"
    assert course["status"] == "פתוח לרישום"
    assert course["start_date"] == "2026-06-19"
    assert course["schedule_note"] == "These are calendar dates only; no class hours are supplied by MyBusiness."
    assert course["product_price"] is None
    assert "get_course_current_price" in course["price_note"]


class LanguageDatesService(MyBusinessService):
    def __init__(self) -> None:
        super().__init__(Settings(MYBUSINESS_APP_ID="app", MYBUSINESS_MASTER_KEY="key"))

    async def list_course_categories(self, search: str | None = None) -> dict:
        return {
            "categories_count": 1,
            "categories": [{"category_id": "forklift", "name": "מלגזה", "code": "80001"}],
        }

    async def _get_open_statuses(self) -> list[dict]:
        return [{"status_id": "open", "name": "פתוח", "is_open": True}]

    async def _get_class(self, table_name: str, params: dict) -> list[dict]:
        assert table_name == "Courses"
        common = {
            "StartDate": {"__type": "Date", "iso": "2999-08-20T09:00:00.000Z"},
            "EndDate": {"__type": "Date", "iso": "2999-08-21T09:00:00.000Z"},
            "MaxCapacity": 30,
            "RegisteredStudents": 1,
            "StatusId": {"objectId": "open", "Name": "פתוח"},
            "ProductCategory": {"objectId": "forklift", "Name": "מלגזה", "Code": "80001"},
        }
        return [
            {**common, "objectId": "english", "Name": "קורס מלגזה בשפה האנגלית"},
            {**common, "objectId": "thai", "Name": "קורס מלגזה לתאילנדים"},
            {**common, "objectId": "russian", "Name": "קורס מלגזה בשפה הרוסית"},
        ]


def test_category_date_lookup_filters_requested_language() -> None:
    result = asyncio.run(LanguageDatesService().find_available_course_dates(category_name="מלגזה באנגלית"))

    assert result["available_courses_count"] == 1
    assert result["courses"][0]["course_id"] == "english"


def test_forklift_refresher_language_uses_matching_theory_day() -> None:
    result = asyncio.run(LanguageDatesService().find_available_course_dates(category_name="רענון מלגזה בתאית"))

    assert result["available_courses_count"] == 1
    assert result["courses"][0]["course_id"] == "thai"
    assert result["courses"][0]["attendance_option"] == "forklift_refresher_theory_day"


def test_validate_course_for_registration_blocks_external_course() -> None:
    row = {
        "objectId": "course1",
        "Name": "Forklift course",
        "StartDate": {"__type": "Date", "iso": "2999-06-19T09:00:00.000Z"},
        "MaxCapacity": 10,
        "RegisteredStudents": 1,
        "StatusId": {"objectId": "U3IMyC5c9H", "Name": "Open", "IsOpen": True},
        "ExternalCourse": pointer("C_YesNoList", EXTERNAL_COURSE_YES_ID),
    }

    assert validate_course_for_registration(row) == ["COURSE_IS_EXTERNAL"]
