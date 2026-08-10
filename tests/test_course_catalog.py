from app.services.course_catalog import detect_course_language, resolve_course_category_code, text_matches_language


def test_specific_work_at_height_aliases_precede_regular_course() -> None:
    assert resolve_course_category_code("קורס מדריכי עבודה בגובה") == "80017"
    assert resolve_course_category_code("ריענון מדריכי עבודה בגובה") == "80018"
    assert resolve_course_category_code("קורס עבודה בגובה ראשוני") == "80015"


def test_hazmat_manager_course_and_refresher_are_distinct() -> None:
    assert resolve_course_category_code('קורס אחראי שינוע חומ"ס') == "80051"
    assert resolve_course_category_code('רענון אחראי שינוע חומ״ס') == "80052"


def test_thai_language_aliases_match_course_names() -> None:
    assert detect_course_language("קורס מלגזה בתאית") == "תאית"
    assert text_matches_language("קורס מלגזה לתאילנדים", "תאית")
    assert not text_matches_language("קורס מלגזה ברוסית", "תאית")
