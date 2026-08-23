from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any

import httpx

from app.config import Settings
from app.services.course_catalog import (
    detect_course_language,
    normalize_catalog_text,
    resolve_course_category_code,
    text_matches_language,
)


ACCOUNT_MATCH_FIELDS = ("PhoneNumber", "StudentPhone", "Phone2", "Phone3", "StudentId", "IdClient", "CompanyId")
REGISTERED_ENROLLMENT_STATUS_ID = "0BbaSYbE8x"
TENTATIVE_COURSE_STATUS_ID = "bh0iCW38FE"
OPEN_REGISTRATION_COURSE_STATUS_ID = "U3IMyC5c9H"
INACTIVE_COURSE_STATUS_IDS = {"FbdRzAz07C", "d4YY2V8STP", "elArHVxiHv"}
EXTERNAL_COURSE_YES_ID = "mVKQuy9lFi"
EXTERNAL_COURSE_NO_ID = "ZCtaDoTS3G"
WORK_AT_HEIGHT_CATEGORY_CODE = "80015"
WORK_AT_HEIGHT_CATEGORY_NAME = "\u05e2\u05d1\u05d5\u05d3\u05d4 \u05d1\u05d2\u05d5\u05d1\u05d4"
FORKLIFT_CATEGORY_CODE = "80001"
FORKLIFT_CATEGORY_NAME = "\u05de\u05dc\u05d2\u05d6\u05d4"
FORKLIFT_PRACTICAL_CAPACITY = 16
WORK_AT_HEIGHT_SUBJECTS = {
    "ladders": "\u05e1\u05d5\u05dc\u05de\u05d5\u05ea",
    "roofs": "\u05d2\u05d2\u05d5\u05ea",
    "constructions": "\u05e7\u05d5\u05e0\u05e1\u05d8\u05e8\u05d5\u05e7\u05e6\u05d9\u05d4",
    "scaffolds": "\u05e4\u05d9\u05d2\u05d5\u05de\u05d9\u05dd \u05e0\u05d9\u05d9\u05d7\u05d9\u05dd",
    "platforms": "\u05d1\u05d9\u05de\u05d5\u05ea \u05d4\u05e8\u05de\u05d4 \u05de\u05ea\u05e8\u05d5\u05de\u05de\u05d5\u05ea \u05d5\u05e4\u05d9\u05d2\u05d5\u05de\u05d9\u05dd \u05de\u05de\u05d5\u05db\u05e0\u05d9\u05dd",
    "basket": "\u05e1\u05dc\u05d9 \u05d4\u05e8\u05de\u05d4",
    "confined_space": "\u05de\u05e7\u05d5\u05dd \u05de\u05d5\u05e7\u05e3 (\u05db\u05d5\u05dc\u05dc \u05de\u05d9\u05db\u05dc\u05d9\u05d5\u05ea)",
}
MAX_WORK_AT_HEIGHT_SUBJECTS_PER_DAY = 4
MIN_COURSE_KEYWORD_LENGTH = 3
COURSE_SEARCH_STOPWORDS = {
    "course",
    "license",
    "training",
    "קורס",
    "קורסי",
    "קורסים",
    "לימוד",
    "לימודי",
    "לימודים",
    "רישיון",
    "רשיון",
    "היתר",
    "השתלמות",
    "הכשרה",
    "של",
    "על",
    "עם",
    "את",
    "אל",
    "לקורס",
    "בקורס",
    "קציני",
}
PAYMENT_STATUS_IDS = {
    "PAID": "0eBXa9VeT8",
    "PARTIAL": "qXzFm8ABt2",
    "UNPAID": "OvqB17SOkV",
    "COMPANY_INVOICE": "Hhz193kwFu",
}


class MyBusinessService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._registration_locks: dict[str, asyncio.Lock] = {}

    @property
    def is_configured(self) -> bool:
        return bool(self.settings.mybusiness_app_id and self.settings.mybusiness_master_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Parse-Application-Id": self.settings.mybusiness_app_id,
            "X-Parse-Master-Key": self.settings.mybusiness_master_key,
        }

    async def _get_class(self, table_name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.is_configured:
            raise RuntimeError("MyBusiness API credentials are not configured.")

        results: list[dict[str, Any]] = []
        limit = int(params.get("limit") or 1000)
        skip = int(params.get("skip") or 0)
        max_records = int(params.get("max_records") or 0) or None
        base_params = {key: value for key, value in params.items() if key != "max_records"}
        base_params["limit"] = min(limit, max_records) if max_records else limit

        async with httpx.AsyncClient(
            base_url=self.settings.mybusiness_base_url.rstrip("/"),
            headers=self._headers(),
            timeout=self.settings.mybusiness_timeout_seconds,
        ) as client:
            while True:
                response = await client.get(f"/classes/{table_name}", params={**base_params, "skip": skip})
                response.raise_for_status()
                batch = response.json().get("results", [])
                results.extend(batch)
                if max_records and len(results) >= max_records:
                    return results[:max_records]
                if len(batch) < int(base_params["limit"]):
                    break
                skip += int(base_params["limit"])

        return results

    async def _get_object(self, table_name: str, object_id: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if not self.is_configured:
            raise RuntimeError("MyBusiness API credentials are not configured.")

        async with httpx.AsyncClient(
            base_url=self.settings.mybusiness_base_url.rstrip("/"),
            headers=self._headers(),
            timeout=self.settings.mybusiness_timeout_seconds,
        ) as client:
            response = await client.get(f"/classes/{table_name}/{object_id}", params=params or {})
            if response.status_code == HTTPStatus.NOT_FOUND:
                return None
            response.raise_for_status()
            return response.json()

    async def _post_class(self, table_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.is_configured:
            raise RuntimeError("MyBusiness API credentials are not configured.")

        async with httpx.AsyncClient(
            base_url=self.settings.mybusiness_base_url.rstrip("/"),
            headers=self._headers(),
            timeout=self.settings.mybusiness_timeout_seconds,
        ) as client:
            response = await client.post(f"/classes/{table_name}", json=payload)
            response.raise_for_status()
            return response.json()

    async def _put_object(self, table_name: str, object_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.is_configured:
            raise RuntimeError("MyBusiness API credentials are not configured.")

        async with httpx.AsyncClient(
            base_url=self.settings.mybusiness_base_url.rstrip("/"),
            headers=self._headers(),
            timeout=self.settings.mybusiness_timeout_seconds,
        ) as client:
            response = await client.put(f"/classes/{table_name}/{object_id}", json=payload)
            response.raise_for_status()
            return response.json()

    async def find_existing_customer(self, identifier: str) -> dict[str, Any]:
        variants = normalize_identifier_variants(identifier)
        if not variants:
            return {"found": False, "match_count": 0, "returned_count": 0, "identifier_variants": [], "customers": []}
        if not self.is_configured:
            return {
                "found": False,
                "match_count": 0,
                "returned_count": 0,
                "identifier_variants": variants,
                "customers": [],
                "message": "MyBusiness API is not configured.",
            }

        clauses = [{field: value} for value in variants for field in ACCOUNT_MATCH_FIELDS]
        rows = await self._get_class(
            "Accounts",
            {
                "where": json_dumps({"$or": clauses}),
                "limit": 1000,
                "keys": ",".join(
                    [
                        "objectId",
                        "Name",
                        "F_name",
                        "L_name",
                        "Email",
                        "PhoneNumber",
                        "StudentPhone",
                        "Phone2",
                        "Phone3",
                        "StudentId",
                        "IdClient",
                        "CompanyId",
                        "isStudent",
                        "IsAccount",
                        "Delete",
                        "createdAt",
                        "updatedAt",
                    ]
                ),
            },
        )
        customers = [map_customer(row) for row in rows]
        return {
            "found": bool(customers),
            "match_count": len(customers),
            "returned_count": len(customers),
            "identifier_variants": variants,
            "customers": customers,
        }

    async def list_course_categories(self, search: str | None = None) -> dict[str, Any]:
        if not self.is_configured:
            return {"categories_count": 0, "categories": [], "message": "MyBusiness API is not configured."}

        rows = await self._get_class(
            "ProductCategories",
            {
                "limit": 1000,
                "order": "Name",
                "keys": "objectId,Name,Code,createdAt,updatedAt",
            },
        )
        categories = [map_category(row) for row in rows]
        if search:
            categories = match_categories(categories, search)
        return {"categories_count": len(categories), "categories": categories}

    async def find_available_course_dates(
        self,
        category_id: str | None = None,
        category_code: str | None = None,
        category_name: str | None = None,
    ) -> dict[str, Any]:
        if not self.is_configured:
            return {"found": False, "available_courses_count": 0, "courses": [], "message": "MyBusiness API is not configured."}

        language = detect_course_language(category_name)
        normalized_request = normalize_catalog_text(category_name)
        forklift_refresher_language_day = bool(
            language
            and language != "עברית"
            and "מלגזה" in normalized_request
            and "רענון" in normalized_request
        )
        if forklift_refresher_language_day and not category_id:
            category_code = FORKLIFT_CATEGORY_CODE
            category_name = None

        category_result = await self._resolve_category(category_id, category_code, category_name)
        if category_result.get("ambiguous") or not category_result.get("category"):
            if category_name and not category_result.get("ambiguous"):
                return await self._find_available_courses_by_name(category_name)
            return {**category_result, "found": False, "courses": []}

        category = category_result["category"]
        open_statuses = await self._get_open_statuses()
        if not open_statuses:
            return {
                "found": False,
                "requires_representative": True,
                "category": category,
                "available_courses_count": 0,
                "courses": [],
            }

        now_iso = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        where = {
            "StartDate": future_course_start_date_filter(now_iso),
            "StatusId": {"$in": [pointer("CourseStatuses", status["status_id"]) for status in open_statuses]},
            "ProductCategory": pointer("ProductCategories", category["category_id"]),
        }
        where = with_internal_courses_only_filter(where)
        rows = await self._get_class(
            "Courses",
            {
                "where": json_dumps(where),
                "limit": 1000,
                "order": "StartDate",
                "include": "StatusId,ProductCategory,ProductId,FacilityId,MainLecturerId,MainClassId,ExternalCourse",
                "keys": (
                    "objectId,Name,StartDate,EndDate,FirstClass,StatusId,ProductCategory,ProductId,FacilityId,"
                    "MainLecturerId,MainClassId,ExternalCourse,MaxCapacity,RegisteredStudents,AllowOverBooking,NumberOfLessons"
                ),
            },
        )
        if language and language != "עברית":
            rows = [row for row in rows if text_matches_language(row.get("Name"), language)]

        courses = []
        for row in rows:
            course = map_available_course(row, category)
            if course is not None:
                if forklift_refresher_language_day:
                    course["attendance_option"] = "forklift_refresher_theory_day"
                    course["language"] = language
                    course["attendance_note"] = "This date is offered for joining the matching-language theory day as a forklift refresher."
                courses.append(course)

        return {
            "found": bool(courses),
            "requires_representative": not bool(courses),
            "category": category,
            "available_courses_count": len(courses),
            "raw_matching_courses_before_capacity_filter": len(rows),
            "forklift_refresher_language_day": forklift_refresher_language_day,
            "courses": courses,
        }

    async def _find_available_courses_by_name(self, course_name: str) -> dict[str, Any]:
        open_statuses = await self._get_open_statuses()
        if not open_statuses:
            return {"found": False, "requires_representative": True, "available_courses_count": 0, "courses": []}

        now_iso = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        where = {
            "StartDate": future_course_start_date_filter(now_iso),
            "StatusId": {"$in": [pointer("CourseStatuses", status["status_id"]) for status in open_statuses]},
        }
        where = with_internal_courses_only_filter(where)
        rows = await self._get_class(
            "Courses",
            {
                "where": json_dumps(where),
                "limit": 1000,
                "order": "StartDate",
                "include": "StatusId,ProductCategory,ProductId,FacilityId,MainLecturerId,MainClassId,ExternalCourse",
                "keys": (
                    "objectId,Name,StartDate,EndDate,FirstClass,StatusId,ProductCategory,ProductId,FacilityId,"
                    "MainLecturerId,MainClassId,ExternalCourse,MaxCapacity,RegisteredStudents,AllowOverBooking,NumberOfLessons"
                ),
            },
        )

        language = detect_course_language(course_name)
        matched_rows = [row for row in rows if course_row_matches_search(row, course_name)]
        if language:
            matched_rows = [row for row in matched_rows if text_matches_language(row.get("Name"), language)]
        courses = []
        for row in matched_rows:
            category = map_category(row.get("ProductCategory") or {}) if isinstance(row.get("ProductCategory"), dict) else None
            if not category:
                continue
            course = map_available_course(row, category)
            if course is not None:
                courses.append(course)

        return {
            "found": bool(courses),
            "requires_representative": not bool(courses),
            "matched_by": "course_name_keywords",
            "search": course_name,
            "available_courses_count": len(courses),
            "raw_matching_courses_before_capacity_filter": len(matched_rows),
            "courses": courses,
        }

    async def check_customer_registration_eligibility(
        self,
        account_id: str,
        course_id: str,
        sale_id: str | None = None,
        allow_tentative_courses: bool = False,
    ) -> dict[str, Any]:
        if not self.is_configured:
            return {
                "can_register": False,
                "blocking_reasons": ["MYBUSINESS_NOT_CONFIGURED"],
                "existing_future_active_enrollments": [],
                "message": "MyBusiness API is not configured.",
            }

        result: dict[str, Any] = {
            "can_register": False,
            "blocking_reasons": [],
            "account": None,
            "course": None,
            "sale": None,
            "existing_future_active_enrollments": [],
        }

        account = await self.get_account(account_id)
        if account is None:
            return {**result, "blocking_reasons": ["ACCOUNT_NOT_FOUND"]}
        result["account"] = summarize_account(account)
        if account.get("Delete") is True:
            return {**result, "blocking_reasons": ["ACCOUNT_DELETED"]}

        course = await self.get_course(course_id)
        if course is None:
            return {**result, "blocking_reasons": ["COURSE_NOT_FOUND"]}
        result["course"] = summarize_course(course)

        course_blockers = validate_course_for_registration(course, allow_tentative_courses)
        if course_blockers:
            return {**result, "blocking_reasons": course_blockers}

        existing_enrollments = await self.get_future_active_enrollments(account_id)
        result["existing_future_active_enrollments"] = existing_enrollments
        if existing_enrollments:
            return {**result, "blocking_reasons": ["CUSTOMER_ALREADY_HAS_FUTURE_ACTIVE_ENROLLMENT"]}

        if sale_id:
            sale = await self.get_sale(sale_id)
            if sale is None:
                return {**result, "blocking_reasons": ["SALE_NOT_FOUND"]}
            result["sale"] = summarize_sale(sale, account_id)
            if not sale_belongs_to_account(sale, account_id):
                return {**result, "blocking_reasons": ["SALE_DOES_NOT_BELONG_TO_ACCOUNT"]}

        result["can_register"] = True
        return result

    async def register_customer_to_course(
        self,
        account_id: str,
        course_id: str,
        sale_id: str,
        payment_status: str | None = None,
        amount_paid: float = 0,
        comment: str | None = None,
        allow_tentative_courses: bool = False,
        dry_run: bool = False,
        high_work_subjects: str | list[str] | None = None,
        work_at_height_training_type: str | None = None,
    ) -> dict[str, Any]:
        requested_payment_status = (payment_status or "UNPAID").strip().upper()
        if requested_payment_status not in PAYMENT_STATUS_IDS:
            return {
                "created": False,
                "dry_run": dry_run,
                "eligibility": {"can_register": False, "blocking_reasons": ["INVALID_PAYMENT_STATUS"]},
            }
        lock = self._registration_locks.setdefault(course_id, asyncio.Lock())
        async with lock:
            return await self._register_customer_to_course_locked(
                account_id=account_id,
                course_id=course_id,
                sale_id=sale_id,
                requested_payment_status=requested_payment_status,
                amount_paid=amount_paid,
                comment=comment,
                allow_tentative_courses=allow_tentative_courses,
                dry_run=dry_run,
                high_work_subjects=high_work_subjects,
                work_at_height_training_type=work_at_height_training_type,
            )

    async def _register_customer_to_course_locked(
        self,
        *,
        account_id: str,
        course_id: str,
        sale_id: str,
        requested_payment_status: str,
        amount_paid: float,
        comment: str | None,
        allow_tentative_courses: bool,
        dry_run: bool,
        high_work_subjects: str | list[str] | None,
        work_at_height_training_type: str | None,
    ) -> dict[str, Any]:
        eligibility = await self.check_customer_registration_eligibility(
            account_id=account_id,
            course_id=course_id,
            sale_id=sale_id,
            allow_tentative_courses=allow_tentative_courses,
        )
        if not eligibility.get("can_register"):
            return {"created": False, "dry_run": dry_run, "eligibility": eligibility}

        latest_course = await self.get_course(course_id)
        if latest_course is None:
            eligibility = {**eligibility, "can_register": False, "blocking_reasons": ["COURSE_NOT_FOUND"]}
            return {"created": False, "dry_run": dry_run, "eligibility": eligibility}

        latest_blockers = validate_course_for_registration(latest_course, allow_tentative_courses)
        if latest_blockers:
            eligibility = {**eligibility, "can_register": False, "blocking_reasons": latest_blockers}
            return {"created": False, "dry_run": dry_run, "eligibility": eligibility}

        payment_verification = None
        effective_payment_status = requested_payment_status
        effective_amount_paid = amount_paid
        if not dry_run:
            payment_verification = await self.verify_sale_payment_for_course(
                sale_id=sale_id,
                account_id=account_id,
                course=latest_course,
            )
            if not payment_verification.get("verified"):
                eligibility = {
                    **eligibility,
                    "can_register": False,
                    "blocking_reasons": [payment_verification["blocking_reason"]],
                }
                return {
                    "created": False,
                    "dry_run": False,
                    "eligibility": eligibility,
                    "payment_verification": payment_verification,
                }
            effective_payment_status = str(payment_verification["payment_status"])
            effective_amount_paid = float(payment_verification["amount_paid"])

        normalized_high_work_subjects = normalize_high_work_subjects(high_work_subjects)
        account_update_payload: dict[str, Any] | None = None
        previous_high_work_subjects: Any = None
        if is_work_at_height_course(latest_course):
            normalized_training_type = normalize_work_at_height_training_type(work_at_height_training_type)
            is_refresher = normalized_training_type == "REFRESHER" or (
                normalized_training_type is None and is_work_at_height_refresher(latest_course)
            )
            if not normalized_high_work_subjects:
                eligibility = {**eligibility, "can_register": False, "blocking_reasons": ["HIGH_WORK_SUBJECTS_REQUIRED"]}
                return {
                    "created": False,
                    "dry_run": dry_run,
                    "eligibility": eligibility,
                    "required_high_work_subjects": list(WORK_AT_HEIGHT_SUBJECTS.values()),
                }
            selected_subjects = [item.strip() for item in normalized_high_work_subjects.split(",") if item.strip()]
            if len(selected_subjects) > MAX_WORK_AT_HEIGHT_SUBJECTS_PER_DAY and not is_refresher:
                eligibility = {**eligibility, "can_register": False, "blocking_reasons": ["TOO_MANY_HIGH_WORK_SUBJECTS"]}
                return {
                    "created": False,
                    "dry_run": dry_run,
                    "eligibility": eligibility,
                    "maximum_high_work_subjects_per_day": MAX_WORK_AT_HEIGHT_SUBJECTS_PER_DAY,
                    "selected_high_work_subjects": selected_subjects,
                    "required_high_work_subjects": list(WORK_AT_HEIGHT_SUBJECTS.values()),
                }
            account = await self.get_account(account_id)
            previous_high_work_subjects = account.get("HighWorkSubjects") if account else None
            merged_subjects = merge_high_work_subjects(previous_high_work_subjects, normalized_high_work_subjects)
            if merged_subjects != previous_high_work_subjects:
                account_update_payload = {"HighWorkSubjects": merged_subjects}

        forklift_practical_assignment = await self.resolve_forklift_practical_assignment(latest_course)
        if forklift_practical_assignment and not forklift_practical_assignment.get("selected_actual_date"):
            eligibility = {
                **eligibility,
                "can_register": False,
                "blocking_reasons": [forklift_practical_assignment["blocking_reason"]],
            }
            return {
                "created": False,
                "dry_run": dry_run,
                "eligibility": eligibility,
                "forklift_practical_assignment": forklift_practical_assignment,
            }

        payload = build_course_enrollment_payload(
            account_id=account_id,
            course=latest_course,
            sale_id=sale_id,
            payment_status=effective_payment_status,
            amount_paid=effective_amount_paid,
            comment=comment,
            actual_date=(
                forklift_practical_assignment.get("selected_actual_date") if forklift_practical_assignment else None
            ),
        )
        if dry_run:
            return {
                "created": False,
                "dry_run": True,
                "eligibility": eligibility,
                "would_create_payload": payload,
                "would_update_account_payload": account_update_payload,
                "forklift_practical_assignment": forklift_practical_assignment,
            }

        account_update_result = None
        if account_update_payload:
            account_update_result = await self.update_account(account_id, account_update_payload)
        try:
            created = await self.create_course_enrollment(payload)
            enrollment_id = created.get("objectId")
            if not enrollment_id:
                raise RuntimeError("MyBusiness did not return an enrollment id.")
        except Exception:
            if account_update_payload:
                await self.update_account(account_id, {"HighWorkSubjects": previous_high_work_subjects})
            raise

        created_enrollment = await self.read_course_enrollment(enrollment_id)
        return {
            "created": True,
            "dry_run": False,
            "eligibility": eligibility,
            "payment_verification": payment_verification,
            "updated_account": account_update_result,
            "created_enrollment": summarize_enrollment(created_enrollment or created),
            "forklift_practical_assignment": forklift_practical_assignment,
        }

    async def get_account(self, account_id: str) -> dict[str, Any] | None:
        return await self._get_object("Accounts", account_id)

    async def update_account(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._put_object("Accounts", account_id, payload)

    async def get_course(self, course_id: str) -> dict[str, Any] | None:
        where = with_internal_courses_only_filter({"objectId": course_id})
        rows = await self._get_class(
            "Courses",
            {
                "where": json_dumps(where),
                "limit": 1,
                "include": "StatusId,ProductCategory,ProductId,FacilityId,MainLecturerId,MainClassId,ExternalCourse",
                "keys": (
                    "objectId,Name,StartDate,EndDate,FirstClass,StatusId,ProductCategory,ProductId,FacilityId,"
                    "MainLecturerId,MainClassId,ExternalCourse,MaxCapacity,RegisteredStudents,AllowOverBooking,NumberOfLessons"
                ),
            },
        )
        return rows[0] if rows else None

    async def get_sale(self, sale_id: str) -> dict[str, Any] | None:
        return await self._get_object(
            "Sales",
            sale_id,
            {
                "include": "AccountId,SaleStatusId",
                "keys": (
                    "objectId,AccountId,SaleStatusId,Total,TotalIncludingVat,AmountPaid,"
                    "IsInvoiced,SignedPriceQuote,createdAt,updatedAt"
                ),
            },
        )

    async def get_sale_rows(self, sale_id: str) -> list[dict[str, Any]]:
        return await self._get_class(
            "SaleRows",
            {
                "where": json_dumps({"SaleId": pointer("Sales", sale_id)}),
                "limit": 1000,
                "include": "SaleId,ProductId,CategoryId,AccountId",
                "keys": "objectId,SaleId,ProductId,CategoryId,AccountId,Quantity,PricePerUnit,Total,TotalIncludingVat",
            },
        )

    async def verify_sale_payment_for_course(
        self,
        *,
        sale_id: str,
        account_id: str,
        course: dict[str, Any],
    ) -> dict[str, Any]:
        sale = await self.get_sale(sale_id)
        if sale is None:
            return payment_verification_failure("SALE_NOT_FOUND")
        if not sale_belongs_to_account(sale, account_id):
            return payment_verification_failure("SALE_DOES_NOT_BELONG_TO_ACCOUNT")

        sale_rows = await self.get_sale_rows(sale_id)
        if not any(sale_row_matches_course(row, course) for row in sale_rows):
            return payment_verification_failure("SALE_DOES_NOT_MATCH_COURSE")

        amount_paid = number_value(sale.get("AmountPaid")) or 0.0
        total = number_value(sale.get("TotalIncludingVat"))
        if total is None:
            total = number_value(sale.get("Total"))

        if sale.get("IsInvoiced") is True:
            payment_status = "COMPANY_INVOICE"
        elif amount_paid <= 0:
            return payment_verification_failure("PAYMENT_NOT_VERIFIED_BY_SYSTEM")
        elif total is not None and total > 0 and amount_paid + 0.01 >= total:
            payment_status = "PAID"
        else:
            payment_status = "PARTIAL"

        return {
            "verified": True,
            "source": "MyBusiness.Sales",
            "sale_id": sale_id,
            "payment_status": payment_status,
            "amount_paid": amount_paid,
            "sale_total": total,
        }

    async def find_verified_course_payment(self, identifier: str, course_id: str) -> dict[str, Any]:
        customers = await self.find_existing_customer(identifier)
        course = await self.get_course(course_id)
        if course is None:
            return {"verified": False, "blocking_reason": "COURSE_NOT_FOUND"}

        for customer in customers.get("customers") or []:
            account_id = customer.get("account_id")
            if not account_id:
                continue
            sales = await self._get_class(
                "Sales",
                {
                    "where": json_dumps({"AccountId": pointer("Accounts", account_id)}),
                    "limit": 50,
                    "order": "-createdAt",
                    "include": "AccountId,SaleStatusId",
                    "keys": (
                        "objectId,AccountId,SaleStatusId,Total,TotalIncludingVat,AmountPaid,"
                        "IsInvoiced,SignedPriceQuote,createdAt,updatedAt"
                    ),
                    "max_records": 50,
                },
            )
            for sale in sales:
                sale_id = sale.get("objectId")
                if not sale_id:
                    continue
                verification = await self.verify_sale_payment_for_course(
                    sale_id=sale_id,
                    account_id=account_id,
                    course=course,
                )
                if verification.get("verified"):
                    return {**verification, "account_id": account_id, "course_id": course_id}

        return {
            "verified": False,
            "blocking_reason": "VERIFIED_COURSE_PAYMENT_NOT_FOUND",
            "source": "MyBusiness.Sales",
        }

    async def get_future_active_enrollments(self, account_id: str) -> list[dict[str, Any]]:
        rows = await self._get_class(
            "CourseEnrollment",
            {
                "where": json_dumps({"AccountId": pointer("Accounts", account_id)}),
                "limit": 1000,
                "include": "CourseId,CourseId.StatusId,CourseId.ProductCategory,CourseId.ProductId,SaleId,CourseEnrollmentStatusId,PayingStatus",
                "keys": (
                    "objectId,AccountId,CourseId,SaleId,CourseEnrollmentStatusId,PayingStatus,"
                    "CourseId.objectId,CourseId.Name,CourseId.StartDate,CourseId.StatusId"
                ),
            },
        )
        enrollments = []
        for row in rows:
            if is_future_active_enrollment(row):
                enrollments.append(summarize_existing_enrollment(row))
        return enrollments

    async def resolve_forklift_practical_assignment(self, course: dict[str, Any]) -> dict[str, Any] | None:
        if not is_forklift_course(course):
            return None

        course_id = course.get("objectId")
        if not course_id:
            return {
                "required": True,
                "selected_actual_date": None,
                "blocking_reason": "FORKLIFT_COURSE_ID_MISSING",
                "options": [],
                "capacity_per_practical_date": FORKLIFT_PRACTICAL_CAPACITY,
            }

        rows = await self._get_class(
            "CourseEnrollment",
            {
                "where": json_dumps({"CourseId": pointer("Courses", course_id)}),
                "limit": 1000,
                "include": "CourseEnrollmentStatusId",
                "keys": "objectId,ActualDate,CourseEnrollmentStatusId,CourseId",
            },
        )

        day_counts: dict[str, int] = {}
        for row in rows:
            day_key = calendar_date_key(row.get("ActualDate"))
            if not day_key:
                continue
            day_counts.setdefault(day_key, 0)
            if is_registered_enrollment(row):
                day_counts[day_key] += 1

        # EndDate is the only course-level fallback available when the second
        # practical date has not received its first enrollment yet.
        start_day = calendar_date_key(course.get("StartDate"))
        end_day = calendar_date_key(course.get("EndDate"))
        if end_day and end_day != start_day:
            day_counts.setdefault(end_day, 0)

        options = [
            {
                "actual_date": actual_date_from_day_key(day_key),
                "registered_students": registered_students,
                "capacity": FORKLIFT_PRACTICAL_CAPACITY,
                "available_seats": max(0, FORKLIFT_PRACTICAL_CAPACITY - registered_students),
                "is_full": registered_students >= FORKLIFT_PRACTICAL_CAPACITY,
            }
            for day_key, registered_students in sorted(day_counts.items())
        ]
        selected = next((option for option in options if not option["is_full"]), None)

        if selected:
            return {
                "required": True,
                "selected_actual_date": selected["actual_date"],
                "selected": selected,
                "options": options,
                "capacity_per_practical_date": FORKLIFT_PRACTICAL_CAPACITY,
            }

        return {
            "required": True,
            "selected_actual_date": None,
            "blocking_reason": (
                "FORKLIFT_PRACTICAL_DATES_FULL" if options else "FORKLIFT_PRACTICAL_DATES_NOT_FOUND"
            ),
            "options": options,
            "capacity_per_practical_date": FORKLIFT_PRACTICAL_CAPACITY,
        }

    async def create_course_enrollment(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_class("CourseEnrollment", payload)

    async def read_course_enrollment(self, enrollment_id: str) -> dict[str, Any] | None:
        return await self._get_object(
            "CourseEnrollment",
            enrollment_id,
            {"include": "AccountId,CourseId,CourseId.StatusId,SaleId,CourseEnrollmentStatusId,PayingStatus"},
        )

    async def _resolve_category(self, category_id: str | None, category_code: str | None, category_name: str | None) -> dict[str, Any]:
        categories = (await self.list_course_categories()).get("categories", [])
        if category_id:
            matches = [category for category in categories if category["category_id"] == category_id]
        elif category_code:
            normalized_code = normalize_text(category_code)
            matches = [category for category in categories if normalize_text(category["code"]) == normalized_code]
        elif category_name:
            alias_code = resolve_course_category_code(category_name)
            matches = [category for category in categories if normalize_text(category["code"]) == alias_code] if alias_code else []
            if not matches:
                matches = match_categories(categories, category_name)
        else:
            return {"found": False, "message": "Missing category_id, category_code, or category_name."}

        if len(matches) == 1:
            return {"found": True, "category": matches[0]}
        if len(matches) > 1:
            return {"found": False, "ambiguous": True, "matches": matches, "courses": []}
        return {
            "found": False,
            "requires_representative": True,
            "message": "No matching course category found after exact, partial, and keyword search.",
            "matches": [],
        }

    async def _get_open_statuses(self) -> list[dict[str, Any]]:
        rows = await self._get_class(
            "CourseStatuses",
            {
                "where": json_dumps({"IsOpen": True}),
                "limit": 1000,
                "keys": "objectId,Name,IsOpen",
            },
        )
        return [{"status_id": row.get("objectId"), "name": clean(row.get("Name")), "is_open": bool(row.get("IsOpen"))} for row in rows]


def json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def pointer(class_name: str, object_id: str) -> dict[str, str]:
    return {"__type": "Pointer", "className": class_name, "objectId": object_id}


def with_internal_courses_only_filter(where: dict[str, Any]) -> dict[str, Any]:
    return {
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


def is_external_course(row: dict[str, Any]) -> bool:
    external_course = row.get("ExternalCourse")
    if not isinstance(external_course, dict):
        return False
    return external_course.get("objectId") == EXTERNAL_COURSE_YES_ID or clean(external_course.get("Name")) == "כן"


def is_work_at_height_course(row: dict[str, Any]) -> bool:
    category = row.get("ProductCategory") if isinstance(row.get("ProductCategory"), dict) else {}
    name = normalize_text(category.get("Name"))
    code = normalize_text(category.get("Code"))
    course_name = normalize_text(row.get("Name"))
    return code == WORK_AT_HEIGHT_CATEGORY_CODE or name == WORK_AT_HEIGHT_CATEGORY_NAME or WORK_AT_HEIGHT_CATEGORY_NAME in course_name


def is_work_at_height_refresher(row: dict[str, Any]) -> bool:
    category = row.get("ProductCategory") if isinstance(row.get("ProductCategory"), dict) else {}
    product = row.get("ProductId") if isinstance(row.get("ProductId"), dict) else {}
    text = normalize_text(" ".join(str(value or "") for value in (row.get("Name"), category.get("Name"), product.get("Name"))))
    return is_work_at_height_course(row) and any(marker in text for marker in ("רענון", "ריענון"))


def normalize_work_at_height_training_type(value: Any) -> str | None:
    text = normalize_text(value).replace("ריענון", "רענון")
    if text in {"refresher", "refresh", "רענון"}:
        return "REFRESHER"
    if text in {"initial", "first", "ראשוני", "ראשונית"}:
        return "INITIAL"
    return None


def is_forklift_course(row: dict[str, Any]) -> bool:
    category = row.get("ProductCategory") if isinstance(row.get("ProductCategory"), dict) else {}
    name = normalize_text(category.get("Name"))
    code = normalize_text(category.get("Code"))
    course_name = normalize_text(row.get("Name"))
    if code == FORKLIFT_CATEGORY_CODE or name == FORKLIFT_CATEGORY_NAME:
        return True
    excluded_name_markers = ("\u05e8\u05e2\u05e0\u05d5\u05df", "\u05de\u05d3\u05e8\u05d9\u05da", "\u05de\u05d3\u05e8\u05d9\u05db\u05d9")
    return FORKLIFT_CATEGORY_NAME in course_name and not any(marker in course_name for marker in excluded_name_markers)


def normalize_high_work_subjects(value: str | list[str] | None) -> str | None:
    if value is None:
        return None
    raw_items = value if isinstance(value, list) else re.split(r"[,;/|]+|\n+", str(value))
    raw_text = normalize_text(" ".join(str(item) for item in raw_items))
    if not raw_text:
        return None

    selected: list[str] = []
    subject_aliases = {
        WORK_AT_HEIGHT_SUBJECTS["ladders"]: ("ladder", "\u05e1\u05d5\u05dc\u05dd", "\u05e1\u05d5\u05dc\u05de\u05d5\u05ea"),
        WORK_AT_HEIGHT_SUBJECTS["roofs"]: ("roof", "\u05d2\u05d2", "\u05d2\u05d2\u05d5\u05ea"),
        WORK_AT_HEIGHT_SUBJECTS["constructions"]: (
            "construction",
            "\u05e7\u05d5\u05e0\u05e1\u05d8\u05e8\u05d5\u05e7\u05e6",
            "\u05e7\u05d5\u05e0\u05e1\u05d8\u05e8\u05d5\u05e7\u05e6\u05d9\u05d4",
            "\u05e7\u05d5\u05e0\u05e1\u05d8\u05e8\u05d5\u05e7\u05e6\u05d9\u05d5\u05ea",
        ),
        WORK_AT_HEIGHT_SUBJECTS["scaffolds"]: ("stationary scaffold", "\u05e4\u05d9\u05d2\u05d5\u05dd \u05e0\u05d9\u05d9\u05d7", "\u05e4\u05d9\u05d2\u05d5\u05de\u05d9\u05dd \u05e0\u05d9\u05d9\u05d7\u05d9\u05dd"),
        WORK_AT_HEIGHT_SUBJECTS["platforms"]: (
            "platform",
            "\u05d1\u05de\u05d4",
            "\u05d1\u05de\u05d5\u05ea",
            "\u05d1\u05d9\u05de\u05d5\u05ea",
            "\u05e4\u05d9\u05d2\u05d5\u05dd \u05de\u05de\u05d5\u05db\u05df",
            "\u05e4\u05d9\u05d2\u05d5\u05de\u05d9\u05dd \u05de\u05de\u05d5\u05db\u05e0\u05d9\u05dd",
        ),
        WORK_AT_HEIGHT_SUBJECTS["basket"]: (
            "basket",
            "\u05e1\u05dc\u05d9",
            "\u05e1\u05dc",
            "\u05e1\u05dc\u05d9 \u05d4\u05e8\u05de\u05d4",
        ),
        WORK_AT_HEIGHT_SUBJECTS["confined_space"]: (
            "confined",
            "tank",
            "\u05de\u05e7\u05d5\u05dd \u05de\u05d5\u05e7\u05e3",
            "\u05d7\u05dc\u05dc \u05de\u05d5\u05e7\u05e3",
            "\u05de\u05d9\u05db\u05dc",
            "\u05de\u05db\u05dc",
            "\u05de\u05d9\u05db\u05dc\u05d9\u05d5\u05ea",
            "\u05de\u05db\u05dc\u05d9\u05d5\u05ea",
        ),
    }
    for canonical, aliases in subject_aliases.items():
        if any(alias in raw_text for alias in aliases):
            selected.append(canonical)

    if not selected:
        return None
    return ", ".join(dict.fromkeys(selected))


def merge_high_work_subjects(existing: Any, selected: str) -> str:
    existing_normalized = normalize_high_work_subjects(str(existing or ""))
    combined = ", ".join(item for item in (existing_normalized, selected) if item)
    return normalize_high_work_subjects(combined) or selected


def future_course_start_date_filter(now_iso: str) -> dict[str, Any]:
    return {"$gt": {"__type": "Date", "iso": now_iso}}


def clean(value: Any) -> Any:
    return html.unescape(value) if isinstance(value, str) else value


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or "")).strip().lower())


def normalize_course_search(value: Any) -> str:
    text = normalize_text(value)
    text = re.sub(r"\b(course|license|training)\b", " ", text)
    text = re.sub(r"\bקורס(?:י|ים)?\b", " ", text)
    text = re.sub(r"\bלימוד(?:י|ים)?\b", " ", text)
    text = re.sub(r"\bרישיון\b", " ", text)
    text = re.sub(r"\bהיתר\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def course_search_keywords(value: Any) -> list[str]:
    normalized = normalize_course_search(value)
    normalized = re.sub(r"[/|,;:()\[\]{}\-–—]+", " ", normalized)
    tokens = normalized.split()
    keywords: list[str] = []
    for token in tokens:
        token = token.strip('"׳״`').strip()
        if len(token) < MIN_COURSE_KEYWORD_LENGTH or token in COURSE_SEARCH_STOPWORDS:
            continue
        if token not in keywords:
            keywords.append(token)
    return keywords


def match_categories(categories: list[dict[str, Any]], search: str) -> list[dict[str, Any]]:
    needle = normalize_text(search)
    canonical_needle = normalize_course_search(search)
    if not needle:
        return []

    exact_matches = [
        category
        for category in categories
        if needle == normalize_text(category["code"])
        or needle == normalize_text(category["name"])
        or canonical_needle == normalize_course_search(category["name"])
    ]
    if exact_matches:
        return exact_matches

    keywords = course_search_keywords(search)
    partial_matches = [
        category
        for category in categories
        if keywords and (needle in normalize_text(category["name"]) or needle in normalize_text(category["code"]))
    ]
    if partial_matches:
        return partial_matches

    if not keywords:
        return []
    scored_matches = []
    for category in categories:
        searchable = f"{normalize_course_search(category['name'])} {normalize_text(category['code'])}"
        matched_keywords = [keyword for keyword in keywords if keyword in searchable]
        if matched_keywords:
            scored_matches.append((len(matched_keywords), category))

    if not scored_matches:
        return []
    max_score = max(score for score, _category in scored_matches)
    if max_score < min(2, len(keywords)):
        return []
    return [category for score, category in scored_matches if score == max_score]


def course_row_matches_search(row: dict[str, Any], search: str) -> bool:
    keywords = course_search_keywords(search)
    if not keywords:
        return False

    category = row.get("ProductCategory") if isinstance(row.get("ProductCategory"), dict) else {}
    product = row.get("ProductId") if isinstance(row.get("ProductId"), dict) else {}
    searchable = " ".join(
        [
            normalize_course_search(row.get("Name")),
            normalize_course_search(product.get("Name")),
            normalize_course_search(category.get("Name")),
            normalize_text(category.get("Code")),
        ]
    )
    matched_keywords = [keyword for keyword in keywords if keyword in searchable]
    if not matched_keywords:
        return False
    if len(matched_keywords) >= 2:
        return True
    # A single distinctive keyword is enough for course-name fallback. Generic terms are removed by course_search_keywords.
    return len(keywords) <= 2


def normalize_identifier_variants(identifier: str) -> list[str]:
    raw = str(identifier or "").strip()
    digits = re.sub(r"\D+", "", raw)
    variants = []
    for value in (raw, digits):
        if value and value not in variants:
            variants.append(value)

    if digits.startswith("972") and len(digits) >= 11:
        local = "0" + digits[3:]
        no_zero = digits[3:]
        for value in (local, no_zero, digits, f"+{digits}"):
            if value not in variants:
                variants.append(value)
    elif digits.startswith("0") and len(digits) >= 9:
        no_zero = digits[1:]
        intl = f"972{no_zero}"
        for value in (no_zero, intl, f"+{intl}"):
            if value not in variants:
                variants.append(value)

    return variants


def map_customer(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_id": row.get("objectId"),
        "name": clean(row.get("Name")),
        "first_name": clean(row.get("F_name")),
        "last_name": clean(row.get("L_name")),
        "email": mask_email(clean(row.get("Email"))),
        "phone_number": mask_phone(clean(row.get("PhoneNumber"))),
        "student_phone": mask_phone(clean(row.get("StudentPhone"))),
        "phone2": mask_phone(clean(row.get("Phone2"))),
        "phone3": mask_phone(clean(row.get("Phone3"))),
        "student_id": mask_identifier(clean(row.get("StudentId"))),
        "id_client": mask_identifier(clean(row.get("IdClient"))),
        "company_id": mask_identifier(clean(row.get("CompanyId"))),
        "is_student": row.get("isStudent"),
        "is_account": row.get("IsAccount"),
        "deleted": row.get("Delete"),
        "created_at": row.get("createdAt"),
        "updated_at": row.get("updatedAt"),
    }


def map_category(row: dict[str, Any]) -> dict[str, Any]:
    name = clean(row.get("Name")) or ""
    return {
        "category_id": row.get("objectId"),
        "name": name,
        "code": clean(row.get("Code")),
        "normalized_name": normalize_text(name),
        "created_at": row.get("createdAt"),
        "updated_at": row.get("updatedAt"),
    }


def map_available_course(row: dict[str, Any], category: dict[str, Any]) -> dict[str, Any] | None:
    if is_external_course(row):
        return None

    max_capacity = row.get("MaxCapacity")
    if max_capacity is None:
        return None
    registered_students = row.get("RegisteredStudents") or 0
    available_seats = int(max_capacity) - int(registered_students)
    if available_seats <= 0:
        return None

    status = row.get("StatusId") or {}
    product = row.get("ProductId") or {}
    facility = row.get("FacilityId") or {}
    lecturer = row.get("MainLecturerId") or {}
    class_obj = row.get("MainClassId") or {}
    return {
        "course_id": row.get("objectId"),
        "course_name": clean(row.get("Name")),
        # MyBusiness stores calendar dates with a placeholder hour. Never expose
        # that timestamp as the actual class time.
        "start_date": calendar_date_key(row.get("StartDate")),
        "end_date": calendar_date_key(row.get("EndDate")),
        "first_class": calendar_date_key(row.get("FirstClass")),
        "schedule_note": "These are calendar dates only; no class hours are supplied by MyBusiness.",
        "available_seats": available_seats,
        "max_capacity": max_capacity,
        "registered_students": registered_students,
        "allow_overbooking": row.get("AllowOverBooking"),
        "status": clean(status.get("Name")) if isinstance(status, dict) else None,
        "status_id": status.get("objectId") if isinstance(status, dict) else None,
        "facility": clean(facility.get("Name")) if isinstance(facility, dict) else None,
        "facility_id": facility.get("objectId") if isinstance(facility, dict) else None,
        "lecturer": clean(lecturer.get("Name")) if isinstance(lecturer, dict) else None,
        "lecturer_id": lecturer.get("objectId") if isinstance(lecturer, dict) else None,
        "class_name": clean(class_obj.get("Name")) if isinstance(class_obj, dict) else None,
        "class_id": class_obj.get("objectId") if isinstance(class_obj, dict) else None,
        "product_id": product.get("objectId") if isinstance(product, dict) else None,
        "product_name": clean(product.get("Name")) if isinstance(product, dict) else None,
        "product_price": None,
        "price_note": "Current prices must be resolved with get_course_current_price or get_course_payment_links.",
        "product_catalog_number": clean(product.get("CatalogNumber")) if isinstance(product, dict) else None,
        "category_id": category["category_id"],
        "category_name": category["name"],
        "category_code": category["code"],
        "number_of_lessons": row.get("NumberOfLessons"),
    }


def parse_date(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("iso")
    if isinstance(value, str):
        return value
    return None


def parse_datetime(value: Any) -> datetime | None:
    iso = parse_date(value)
    if not iso:
        return None
    normalized = iso.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def calendar_date_key(value: Any) -> str | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return parsed.date().isoformat()


def actual_date_from_day_key(day_key: str) -> str:
    return f"{day_key}T00:00:00.000Z"


def calculate_available_seats(course: dict[str, Any]) -> int | None:
    max_capacity = course.get("MaxCapacity")
    if max_capacity is None:
        return None
    try:
        return int(max_capacity) - int(course.get("RegisteredStudents") or 0)
    except (TypeError, ValueError):
        return None


def validate_course_for_registration(course: dict[str, Any], allow_tentative_courses: bool = False) -> list[str]:
    blockers: list[str] = []
    start_date = parse_datetime(course.get("StartDate"))
    status = course.get("StatusId") if isinstance(course.get("StatusId"), dict) else {}
    status_id = status.get("objectId")
    available_seats = calculate_available_seats(course)

    if start_date is None or start_date <= datetime.now(UTC):
        blockers.append("COURSE_ALREADY_STARTED_OR_PASSED")
    if not status_id or not status.get("IsOpen"):
        blockers.append("COURSE_NOT_OPEN_FOR_REGISTRATION")
    if status_id == TENTATIVE_COURSE_STATUS_ID and not allow_tentative_courses:
        blockers.append("COURSE_IS_TENTATIVE")
    if available_seats is None or available_seats <= 0:
        blockers.append("COURSE_FULL")
    if is_external_course(course):
        blockers.append("COURSE_IS_EXTERNAL")

    return blockers


def is_future_active_enrollment(enrollment: dict[str, Any]) -> bool:
    enrollment_status = enrollment.get("CourseEnrollmentStatusId") or {}
    course = enrollment.get("CourseId") or {}
    course_status = course.get("StatusId") or {}
    course_status_id = course_status.get("objectId")
    start_date = parse_datetime(course.get("StartDate"))
    return (
        isinstance(enrollment_status, dict)
        and enrollment_status.get("objectId") == REGISTERED_ENROLLMENT_STATUS_ID
        and start_date is not None
        and start_date > datetime.now(UTC)
        and isinstance(course_status, dict)
        and course_status.get("IsOpen") is True
        and course_status_id not in INACTIVE_COURSE_STATUS_IDS
    )


def is_registered_enrollment(enrollment: dict[str, Any]) -> bool:
    enrollment_status = enrollment.get("CourseEnrollmentStatusId") or {}
    return isinstance(enrollment_status, dict) and (
        enrollment_status.get("objectId") == REGISTERED_ENROLLMENT_STATUS_ID
        or enrollment_status.get("IsRegistered") is True
    )


def sale_belongs_to_account(sale: dict[str, Any], account_id: str) -> bool:
    account = sale.get("AccountId")
    return isinstance(account, dict) and account.get("objectId") == account_id


def sale_row_matches_course(row: dict[str, Any], course: dict[str, Any]) -> bool:
    row_product = row.get("ProductId") if isinstance(row.get("ProductId"), dict) else {}
    row_category = row.get("CategoryId") if isinstance(row.get("CategoryId"), dict) else {}
    course_product = course.get("ProductId") if isinstance(course.get("ProductId"), dict) else {}
    course_category = course.get("ProductCategory") if isinstance(course.get("ProductCategory"), dict) else {}
    product_id = course_product.get("objectId")
    category_id = course_category.get("objectId")
    return bool(
        (product_id and row_product.get("objectId") == product_id)
        or (category_id and row_category.get("objectId") == category_id)
    )


def number_value(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def payment_verification_failure(reason: str) -> dict[str, Any]:
    return {
        "verified": False,
        "source": "MyBusiness.Sales",
        "blocking_reason": reason,
    }


def summarize_account(account: dict[str, Any]) -> dict[str, Any]:
    name = clean(account.get("Name")) or " ".join(
        item for item in [clean(account.get("F_name")), clean(account.get("L_name"))] if item
    )
    return {"account_id": account.get("objectId"), "name": name or None}


def summarize_course(course: dict[str, Any]) -> dict[str, Any]:
    status = course.get("StatusId") if isinstance(course.get("StatusId"), dict) else {}
    category = course.get("ProductCategory") if isinstance(course.get("ProductCategory"), dict) else {}
    return {
        "course_id": course.get("objectId"),
        "course_name": clean(course.get("Name")),
        "start_date": parse_date(course.get("StartDate")),
        "status": clean(status.get("Name")),
        "status_id": status.get("objectId"),
        "is_open": status.get("IsOpen"),
        "available_seats": calculate_available_seats(course),
        "category_id": category.get("objectId"),
        "category_name": clean(category.get("Name")),
        "category_code": clean(category.get("Code")),
    }


def summarize_sale(sale: dict[str, Any], account_id: str) -> dict[str, Any]:
    status = sale.get("SaleStatusId") if isinstance(sale.get("SaleStatusId"), dict) else {}
    return {
        "sale_id": sale.get("objectId"),
        "status": clean(status.get("Name")),
        "status_id": status.get("objectId"),
        "belongs_to_account": sale_belongs_to_account(sale, account_id),
    }


def summarize_existing_enrollment(enrollment: dict[str, Any]) -> dict[str, Any]:
    course = enrollment.get("CourseId") if isinstance(enrollment.get("CourseId"), dict) else {}
    course_status = course.get("StatusId") if isinstance(course.get("StatusId"), dict) else {}
    paying_status = enrollment.get("PayingStatus") if isinstance(enrollment.get("PayingStatus"), dict) else {}
    return {
        "enrollment_id": enrollment.get("objectId"),
        "course_id": course.get("objectId"),
        "course_name": clean(course.get("Name")),
        "course_start_date": parse_date(course.get("StartDate")),
        "start_date": parse_date(course.get("StartDate")),
        "course_status": clean(course_status.get("Name")),
        "status": clean(course_status.get("Name")),
        "paying_status": clean(paying_status.get("Name")),
    }


def build_course_enrollment_payload(
    account_id: str,
    course: dict[str, Any],
    sale_id: str,
    payment_status: str,
    amount_paid: float = 0,
    comment: str | None = None,
    actual_date: str | None = None,
) -> dict[str, Any]:
    now_iso = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    course_date = parse_date(course.get("StartDate")) or now_iso
    payload = {
        "AccountId": pointer("Accounts", account_id),
        "AccountMainId": pointer("Accounts", account_id),
        "CourseId": pointer("Courses", course["objectId"]),
        "SaleId": pointer("Sales", sale_id),
        "Date": date_value(now_iso),
        "CourseDate": date_value(course_date),
        "FirstClass": date_value(course_date),
        "AmountPaid": amount_paid,
        "Comment": comment or "Created by course registration agent",
        "CourseEnrollmentStatusId": pointer("CourseEnrollmentStatus", REGISTERED_ENROLLMENT_STATUS_ID),
        "PayingStatus": pointer("PayingStatusList", PAYMENT_STATUS_IDS[payment_status]),
        "Files": False,
        "Exam": False,
        "SignedFIle": False,
        "allowWithoutSigned": True,
    }
    if actual_date:
        payload["ActualDate"] = date_value(actual_date)
    return payload


def summarize_enrollment(enrollment: dict[str, Any]) -> dict[str, Any]:
    account = enrollment.get("AccountId") if isinstance(enrollment.get("AccountId"), dict) else {}
    course = enrollment.get("CourseId") if isinstance(enrollment.get("CourseId"), dict) else {}
    sale = enrollment.get("SaleId") if isinstance(enrollment.get("SaleId"), dict) else {}
    enrollment_status = (
        enrollment.get("CourseEnrollmentStatusId") if isinstance(enrollment.get("CourseEnrollmentStatusId"), dict) else {}
    )
    paying_status = enrollment.get("PayingStatus") if isinstance(enrollment.get("PayingStatus"), dict) else {}
    return {
        "enrollment_id": enrollment.get("objectId"),
        "account_id": account.get("objectId"),
        "course_id": course.get("objectId"),
        "sale_id": sale.get("objectId"),
        "course_enrollment_status": clean(enrollment_status.get("Name")),
        "paying_status": clean(paying_status.get("Name")),
        "amount_paid": enrollment.get("AmountPaid"),
    }


def date_value(iso: str) -> dict[str, str]:
    return {"__type": "Date", "iso": iso}


def mask_phone(value: Any) -> str | None:
    text = clean(value)
    if not text:
        return None
    digits = re.sub(r"\D+", "", str(text))
    if len(digits) < 4:
        return "***"
    return f"***{digits[-4:]}"


def mask_identifier(value: Any) -> str | None:
    text = clean(value)
    if not text:
        return None
    digits = re.sub(r"\D+", "", str(text))
    if len(digits) < 4:
        return "***"
    return f"***{digits[-4:]}"


def mask_email(value: Any) -> str | None:
    text = clean(value)
    if not text:
        return None
    email = str(text)
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}***@{domain}"
