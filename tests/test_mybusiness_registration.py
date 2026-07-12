import asyncio
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

from app.services.mybusiness import MyBusinessService, build_course_enrollment_payload, pointer


class FakeMyBusinessService(MyBusinessService):
    def __init__(
        self,
        objects: dict[tuple[str, str], dict],
        class_rows: dict[str, list[dict]] | None = None,
        *,
        fail_posts: bool = False,
    ):
        settings = SimpleNamespace(
            mybusiness_app_id="app",
            mybusiness_master_key="key",
            mybusiness_base_url="https://example.test/parse",
            mybusiness_timeout_seconds=1,
        )
        super().__init__(settings)
        self.objects = objects
        self.class_rows = class_rows or {}
        self.posts: list[tuple[str, dict]] = []
        self.puts: list[tuple[str, str, dict]] = []
        self.fail_posts = fail_posts

    async def _get_object(self, table_name: str, object_id: str, params: dict | None = None) -> dict | None:
        return self.objects.get((table_name, object_id))

    async def _get_class(self, table_name: str, params: dict) -> list[dict]:
        if table_name == "Courses":
            where = json.loads(params.get("where") or "{}")
            course_id = find_object_id_filter(where)
            if course_id:
                course = self.objects.get(("Courses", course_id))
                return [course] if course else []
        if table_name == "CourseEnrollment":
            where = json.loads(params.get("where") or "{}")
            rows = self.class_rows.get(table_name, [])
            account_id = find_pointer_object_id(where, "AccountId")
            course_id = find_pointer_object_id(where, "CourseId")
            if account_id:
                rows = [row for row in rows if pointer_id(row.get("AccountId")) == account_id]
            if course_id:
                rows = [row for row in rows if pointer_id(row.get("CourseId")) == course_id]
            return rows
        if table_name == "SaleRows":
            where = json.loads(params.get("where") or "{}")
            rows = self.class_rows.get(table_name, [])
            sale_id = find_pointer_object_id(where, "SaleId")
            if sale_id:
                rows = [row for row in rows if pointer_id(row.get("SaleId")) == sale_id]
            return rows
        return self.class_rows.get(table_name, [])

    async def _post_class(self, table_name: str, payload: dict) -> dict:
        await asyncio.sleep(0)
        if self.fail_posts:
            raise RuntimeError("simulated post failure")
        self.posts.append((table_name, payload))
        created = {"objectId": f"enrollment{len(self.posts)}", **payload}
        self.class_rows.setdefault(table_name, []).append(created)
        return created

    async def _put_object(self, table_name: str, object_id: str, payload: dict) -> dict:
        self.puts.append((table_name, object_id, payload))
        existing = self.objects.get((table_name, object_id), {"objectId": object_id})
        existing.update(payload)
        self.objects[(table_name, object_id)] = existing
        return {"updatedAt": future_iso(0), **payload}


def future_iso(days: int = 5) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def open_course(course_id: str = "course1") -> dict:
    return {
        "objectId": course_id,
        "Name": "General course",
        "StartDate": {"__type": "Date", "iso": future_iso()},
        "StatusId": {"objectId": "U3IMyC5c9H", "Name": "Open", "IsOpen": True},
        "ProductCategory": {"objectId": "cat1", "Name": "General", "Code": "99999"},
        "ProductId": {"objectId": "product1", "Name": "General product"},
        "MaxCapacity": 10,
        "RegisteredStudents": 7,
    }


def open_forklift_course(course_id: str = "forklift1") -> dict:
    course = open_course(course_id)
    course.update(
        {
            "Name": "\u05e7\u05d5\u05e8\u05e1 \u05de\u05dc\u05d2\u05d6\u05d4",
            "ProductCategory": {"objectId": "cat_forklift", "Name": "\u05de\u05dc\u05d2\u05d6\u05d4", "Code": "80001"},
            "MaxCapacity": 30,
            "RegisteredStudents": 10,
        }
    )
    return course


def paid_sale(sale_id: str = "sale1", account_id: str = "account1") -> dict:
    return {
        "objectId": sale_id,
        "AccountId": {"objectId": account_id},
        "TotalIncludingVat": 100,
        "AmountPaid": 100,
    }


def matching_sale_row(sale_id: str = "sale1", account_id: str = "account1") -> dict:
    return {
        "objectId": f"row-{sale_id}",
        "SaleId": pointer("Sales", sale_id),
        "AccountId": pointer("Accounts", account_id),
        "ProductId": pointer("Products", "product1"),
        "CategoryId": pointer("ProductCategories", "cat1"),
    }


def open_work_at_height_course(course_id: str = "height1") -> dict:
    course = open_course(course_id)
    course.update(
        {
            "Name": "\u05d4\u05d3\u05e8\u05db\u05ea \u05e2\u05d1\u05d5\u05d3\u05d4 \u05d1\u05d2\u05d5\u05d1\u05d4",
            "ProductCategory": {
                "objectId": "cat_height",
                "Name": "\u05e2\u05d1\u05d5\u05d3\u05d4 \u05d1\u05d2\u05d5\u05d1\u05d4",
                "Code": "80015",
            },
        }
    )
    return course


def find_object_id_filter(where):
    if isinstance(where, dict):
        if isinstance(where.get("objectId"), str):
            return where["objectId"]
        for value in where.values():
            if isinstance(value, list):
                for item in value:
                    found = find_object_id_filter(item)
                    if found:
                        return found
            elif isinstance(value, dict):
                found = find_object_id_filter(value)
                if found:
                    return found
    return None


def find_pointer_object_id(where, field_name: str):
    if isinstance(where, dict):
        value = where.get(field_name)
        if isinstance(value, dict) and isinstance(value.get("objectId"), str):
            return value["objectId"]
        for value in where.values():
            if isinstance(value, list):
                for item in value:
                    found = find_pointer_object_id(item, field_name)
                    if found:
                        return found
            elif isinstance(value, dict):
                found = find_pointer_object_id(value, field_name)
                if found:
                    return found
    return None


def pointer_id(value):
    return value.get("objectId") if isinstance(value, dict) else None


def practical_enrollment(index: int, course_id: str, actual_date: str, account_id: str | None = None) -> dict:
    return {
        "objectId": f"practical-{index}",
        "AccountId": pointer("Accounts", account_id or f"student-{index}"),
        "CourseId": pointer("Courses", course_id),
        "ActualDate": {"__type": "Date", "iso": actual_date},
        "CourseEnrollmentStatusId": {"objectId": "0BbaSYbE8x", "Name": "Registered"},
    }


def test_check_customer_registration_eligibility_allows_valid_account_course_and_sale() -> None:
    service = FakeMyBusinessService(
        {
            ("Accounts", "account1"): {"objectId": "account1", "Name": "Test Customer", "Delete": False},
            ("Courses", "course1"): open_course(),
            ("Sales", "sale1"): {
                "objectId": "sale1",
                "AccountId": {"objectId": "account1"},
                "SaleStatusId": {"objectId": "status1", "Name": "New"},
            },
        }
    )

    result = run_async(
        service.check_customer_registration_eligibility(account_id="account1", course_id="course1", sale_id="sale1")
    )

    assert result["can_register"] is True
    assert result["blocking_reasons"] == []
    assert result["course"]["available_seats"] == 3
    assert result["sale"]["belongs_to_account"] is True


def test_check_customer_registration_eligibility_blocks_existing_future_active_enrollment() -> None:
    service = FakeMyBusinessService(
        {
            ("Accounts", "account1"): {"objectId": "account1", "Name": "Test Customer", "Delete": False},
            ("Courses", "course1"): open_course(),
        },
        {
            "CourseEnrollment": [
                {
                    "objectId": "enrollment-existing",
                    "AccountId": pointer("Accounts", "account1"),
                    "CourseEnrollmentStatusId": {"objectId": "0BbaSYbE8x", "Name": "Registered"},
                    "CourseId": open_course("course-other"),
                    "PayingStatus": {"objectId": "0eBXa9VeT8", "Name": "Paid"},
                }
            ]
        },
    )

    result = run_async(service.check_customer_registration_eligibility(account_id="account1", course_id="course1"))

    assert result["can_register"] is False
    assert result["blocking_reasons"] == ["CUSTOMER_ALREADY_HAS_FUTURE_ACTIVE_ENROLLMENT"]
    assert result["existing_future_active_enrollments"][0]["enrollment_id"] == "enrollment-existing"


def test_register_customer_to_course_dry_run_builds_payload_without_posting() -> None:
    service = FakeMyBusinessService(
        {
            ("Accounts", "account1"): {"objectId": "account1", "Name": "Test Customer", "Delete": False},
            ("Courses", "course1"): open_course(),
            ("Sales", "sale1"): {"objectId": "sale1", "AccountId": {"objectId": "account1"}},
        }
    )

    result = run_async(
        service.register_customer_to_course(
            account_id="account1",
            course_id="course1",
            sale_id="sale1",
            payment_status="PAID",
            amount_paid=100,
            dry_run=True,
        )
    )

    assert result["created"] is False
    assert result["dry_run"] is True
    assert service.posts == []
    assert result["would_create_payload"]["PayingStatus"] == pointer("PayingStatusList", "0eBXa9VeT8")
    assert result["would_create_payload"]["AmountPaid"] == 100


def test_forklift_registration_selects_first_practical_date_with_capacity() -> None:
    service = FakeMyBusinessService(
        {
            ("Accounts", "account1"): {"objectId": "account1", "Name": "Test Customer", "Delete": False},
            ("Courses", "forklift1"): open_forklift_course(),
            ("Sales", "sale1"): {"objectId": "sale1", "AccountId": {"objectId": "account1"}},
        },
        {
            "CourseEnrollment": [
                *[practical_enrollment(i, "forklift1", "2026-07-29T09:00:00.000Z") for i in range(15)],
                practical_enrollment(100, "forklift1", "2026-07-30T09:00:00.000Z"),
            ]
        },
    )

    result = run_async(
        service.register_customer_to_course(
            account_id="account1",
            course_id="forklift1",
            sale_id="sale1",
            payment_status="PAID",
            dry_run=True,
        )
    )

    assert result["eligibility"]["can_register"] is True
    assert result["forklift_practical_assignment"]["selected_actual_date"] == "2026-07-29T00:00:00.000Z"
    assert result["would_create_payload"]["ActualDate"] == date_pointer("2026-07-29T00:00:00.000Z")


def test_forklift_registration_skips_full_practical_date_and_selects_second() -> None:
    service = FakeMyBusinessService(
        {
            ("Accounts", "account1"): {"objectId": "account1", "Name": "Test Customer", "Delete": False},
            ("Courses", "forklift1"): open_forklift_course(),
            ("Sales", "sale1"): {"objectId": "sale1", "AccountId": {"objectId": "account1"}},
        },
        {
            "CourseEnrollment": [
                *[practical_enrollment(i, "forklift1", "2026-07-29T00:00:00.000Z") for i in range(8)],
                *[practical_enrollment(20 + i, "forklift1", "2026-07-29T09:00:00.000Z") for i in range(8)],
                practical_enrollment(100, "forklift1", "2026-07-30T09:00:00.000Z"),
            ]
        },
    )

    result = run_async(
        service.register_customer_to_course(
            account_id="account1",
            course_id="forklift1",
            sale_id="sale1",
            payment_status="PAID",
            dry_run=True,
        )
    )

    assert result["eligibility"]["can_register"] is True
    assert result["forklift_practical_assignment"]["selected_actual_date"] == "2026-07-30T00:00:00.000Z"
    assert result["forklift_practical_assignment"]["options"][0]["registered_students"] == 16
    assert result["would_create_payload"]["ActualDate"] == date_pointer("2026-07-30T00:00:00.000Z")


def test_forklift_registration_blocks_when_all_practical_dates_are_full() -> None:
    service = FakeMyBusinessService(
        {
            ("Accounts", "account1"): {"objectId": "account1", "Name": "Test Customer", "Delete": False},
            ("Courses", "forklift1"): open_forklift_course(),
            ("Sales", "sale1"): {"objectId": "sale1", "AccountId": {"objectId": "account1"}},
        },
        {
            "CourseEnrollment": [
                *[practical_enrollment(i, "forklift1", "2026-07-29T09:00:00.000Z") for i in range(16)],
                *[practical_enrollment(20 + i, "forklift1", "2026-07-30T09:00:00.000Z") for i in range(16)],
            ]
        },
    )

    result = run_async(
        service.register_customer_to_course(
            account_id="account1",
            course_id="forklift1",
            sale_id="sale1",
            payment_status="PAID",
            dry_run=True,
        )
    )

    assert result["created"] is False
    assert result["eligibility"]["can_register"] is False
    assert result["eligibility"]["blocking_reasons"] == ["FORKLIFT_PRACTICAL_DATES_FULL"]
    assert "would_create_payload" not in result


def test_work_at_height_registration_requires_subjects() -> None:
    service = FakeMyBusinessService(
        {
            ("Accounts", "account1"): {"objectId": "account1", "Name": "Test Customer", "Delete": False},
            ("Courses", "height1"): open_work_at_height_course(),
            ("Sales", "sale1"): {"objectId": "sale1", "AccountId": {"objectId": "account1"}},
        }
    )

    result = run_async(
        service.register_customer_to_course(
            account_id="account1",
            course_id="height1",
            sale_id="sale1",
            payment_status="PAID",
            dry_run=True,
        )
    )

    assert result["created"] is False
    assert result["eligibility"]["blocking_reasons"] == ["HIGH_WORK_SUBJECTS_REQUIRED"]
    assert result["required_high_work_subjects"] == ["מיכליות", "קונסטרוקציה", "סלי הרמה"]
    assert service.posts == []
    assert service.puts == []


def test_work_at_height_registration_dry_run_includes_account_subject_update() -> None:
    service = FakeMyBusinessService(
        {
            ("Accounts", "account1"): {"objectId": "account1", "Name": "Test Customer", "Delete": False},
            ("Courses", "height1"): open_work_at_height_course(),
            ("Sales", "sale1"): {"objectId": "sale1", "AccountId": {"objectId": "account1"}},
        }
    )

    result = run_async(
        service.register_customer_to_course(
            account_id="account1",
            course_id="height1",
            sale_id="sale1",
            payment_status="PAID",
            dry_run=True,
            high_work_subjects="מיכליות וסלי הרמה",
        )
    )

    assert result["created"] is False
    assert result["dry_run"] is True
    assert result["would_update_account_payload"] == {"HighWorkSubjects": "מיכליות, סלי הרמה"}
    assert service.posts == []
    assert service.puts == []


def test_register_customer_to_course_does_not_post_when_sale_belongs_to_other_account() -> None:
    service = FakeMyBusinessService(
        {
            ("Accounts", "account1"): {"objectId": "account1", "Name": "Test Customer", "Delete": False},
            ("Courses", "course1"): open_course(),
            ("Sales", "sale1"): {"objectId": "sale1", "AccountId": {"objectId": "other-account"}},
        }
    )

    result = run_async(
        service.register_customer_to_course(
            account_id="account1",
            course_id="course1",
            sale_id="sale1",
            payment_status="UNPAID",
            dry_run=False,
        )
    )

    assert result["created"] is False
    assert result["eligibility"]["blocking_reasons"] == ["SALE_DOES_NOT_BELONG_TO_ACCOUNT"]
    assert service.posts == []


def test_register_customer_to_course_blocks_real_write_without_system_payment_verification() -> None:
    service = FakeMyBusinessService(
        {
            ("Accounts", "account1"): {"objectId": "account1", "Name": "Test Customer", "Delete": False},
            ("Courses", "course1"): open_course(),
            ("Sales", "sale1"): {"objectId": "sale1", "AccountId": {"objectId": "account1"}},
        },
        {"SaleRows": [matching_sale_row()]},
    )

    result = run_async(
        service.register_customer_to_course(
            account_id="account1",
            course_id="course1",
            sale_id="sale1",
            payment_status="PAID",
            dry_run=False,
        )
    )

    assert result["created"] is False
    assert result["eligibility"]["blocking_reasons"] == ["PAYMENT_NOT_VERIFIED_BY_SYSTEM"]
    assert service.posts == []


def test_register_customer_to_course_derives_paid_status_from_mybusiness_sale() -> None:
    service = FakeMyBusinessService(
        {
            ("Accounts", "account1"): {"objectId": "account1", "Name": "Test Customer", "Delete": False},
            ("Courses", "course1"): open_course(),
            ("Sales", "sale1"): paid_sale(),
        },
        {"SaleRows": [matching_sale_row()]},
    )

    result = run_async(
        service.register_customer_to_course(
            account_id="account1",
            course_id="course1",
            sale_id="sale1",
            payment_status="UNPAID",
            amount_paid=0,
            dry_run=False,
        )
    )

    assert result["created"] is True
    assert result["payment_verification"]["verified"] is True
    assert result["payment_verification"]["payment_status"] == "PAID"
    assert service.posts[0][1]["PayingStatus"] == pointer("PayingStatusList", "0eBXa9VeT8")
    assert service.posts[0][1]["AmountPaid"] == 100


def test_work_at_height_update_is_rolled_back_when_enrollment_creation_fails() -> None:
    service = FakeMyBusinessService(
        {
            ("Accounts", "account1"): {
                "objectId": "account1",
                "Name": "Test Customer",
                "Delete": False,
                "HighWorkSubjects": "מיכליות",
            },
            ("Courses", "height1"): open_work_at_height_course(),
            ("Sales", "sale1"): paid_sale(),
        },
        {"SaleRows": [matching_sale_row()]},
        fail_posts=True,
    )

    try:
        run_async(
            service.register_customer_to_course(
                account_id="account1",
                course_id="height1",
                sale_id="sale1",
                dry_run=False,
                high_work_subjects="סלי הרמה",
            )
        )
    except RuntimeError as exc:
        assert str(exc) == "simulated post failure"
    else:
        raise AssertionError("Expected enrollment creation to fail")

    assert service.puts[0][2] == {"HighWorkSubjects": "מיכליות, סלי הרמה"}
    assert service.objects[("Accounts", "account1")]["HighWorkSubjects"] == "מיכליות"
    assert service.puts[-1][2] == {"HighWorkSubjects": "מיכליות"}


def test_forklift_practical_assignment_uses_course_end_date_for_empty_second_slot() -> None:
    course = open_forklift_course()
    course["StartDate"] = date_pointer("2026-08-17T09:00:00.000Z")
    course["EndDate"] = date_pointer("2026-08-18T09:00:00.000Z")
    first_date = "2026-08-17T00:00:00.000Z"
    service = FakeMyBusinessService(
        {("Courses", "forklift1"): course},
        {
            "CourseEnrollment": [
                practical_enrollment(index, "forklift1", first_date)
                for index in range(16)
            ]
        },
    )

    result = run_async(service.resolve_forklift_practical_assignment(course))

    assert result["selected_actual_date"] == "2026-08-18T00:00:00.000Z"
    assert result["selected"]["registered_students"] == 0


def test_concurrent_forklift_registrations_do_not_exceed_practical_capacity() -> None:
    course = open_forklift_course()
    course["StartDate"] = date_pointer("2026-08-17T09:00:00.000Z")
    course["EndDate"] = date_pointer("2026-08-18T09:00:00.000Z")
    first_date = "2026-08-17T00:00:00.000Z"
    objects = {
        ("Courses", "forklift1"): course,
        ("Accounts", "account1"): {"objectId": "account1", "Delete": False},
        ("Accounts", "account2"): {"objectId": "account2", "Delete": False},
        ("Sales", "sale1"): paid_sale("sale1", "account1"),
        ("Sales", "sale2"): paid_sale("sale2", "account2"),
    }
    class_rows = {
        "CourseEnrollment": [
            practical_enrollment(index, "forklift1", first_date)
            for index in range(15)
        ],
        "SaleRows": [
            matching_sale_row("sale1", "account1"),
            matching_sale_row("sale2", "account2"),
        ],
    }
    service = FakeMyBusinessService(objects, class_rows)

    async def register_both():
        return await asyncio.gather(
            service.register_customer_to_course(
                account_id="account1",
                course_id="forklift1",
                sale_id="sale1",
                dry_run=False,
            ),
            service.register_customer_to_course(
                account_id="account2",
                course_id="forklift1",
                sale_id="sale2",
                dry_run=False,
            ),
        )

    first, second = run_async(register_both())

    assert first["forklift_practical_assignment"]["selected_actual_date"] == first_date
    assert second["forklift_practical_assignment"]["selected_actual_date"] == "2026-08-18T00:00:00.000Z"


def test_build_course_enrollment_payload_uses_required_pointers() -> None:
    payload = build_course_enrollment_payload(
        account_id="account1",
        course=open_course(),
        sale_id="sale1",
        payment_status="COMPANY_INVOICE",
        amount_paid=0,
        comment=None,
    )

    assert payload["AccountId"] == pointer("Accounts", "account1")
    assert payload["AccountMainId"] == pointer("Accounts", "account1")
    assert payload["CourseId"] == pointer("Courses", "course1")
    assert payload["SaleId"] == pointer("Sales", "sale1")
    assert payload["CourseEnrollmentStatusId"] == pointer("CourseEnrollmentStatus", "0BbaSYbE8x")
    assert payload["PayingStatus"] == pointer("PayingStatusList", "Hhz193kwFu")


def run_async(coro):
    import asyncio

    return asyncio.run(coro)


def date_pointer(iso: str) -> dict:
    return {"__type": "Date", "iso": iso}
