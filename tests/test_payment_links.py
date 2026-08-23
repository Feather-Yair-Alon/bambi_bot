import asyncio
import json
import re
from typing import Any

from app.services.mybusiness import pointer
from app.services.payment_links import (
    PaymentLinkService,
    REQUIRED_CUSTOMER_DETAILS,
    is_mismatched_course_payment_link,
    is_stale_dated_payment_link,
)


def test_heavy_vehicle_category_rejects_crane_payment_button() -> None:
    link = {
        "name": "מקדמה קורס עגורן העמסה עצמית",
        "title": "דף תשלום עגורן העמסה עצמית",
        "description_for_bot": "מקדמה עבור עגורן",
        "product": {"product_name": "עבור תשלום מקדמה לקורס"},
    }

    assert is_mismatched_course_payment_link(
        link,
        {"category_code": "80012", "category_name": "משאית משא כבד C"},
    )
    assert not is_mismatched_course_payment_link(
        link,
        {"category_code": "80009", "category_name": "עגורן העמסה עצמית"},
    )


class FakeMyBusiness:
    is_configured = True

    def __init__(self) -> None:
        self.categories = [
            {"objectId": "cat_forklift", "Name": "מלגזה", "Code": "80001"},
            {"objectId": "cat_forklift_refresh", "Name": "רענון מלגזה", "Code": "80003"},
            {"objectId": "cat_tractor", "Name": "טרקטור", "Code": "80007"},
            {"objectId": "cat_safety_officers_day", "Name": "יום עיון לקציני בטיחות", "Code": "80049"},
            {"objectId": "cat_discount_only", "Name": "קורס הנחות", "Code": "99999"},
            {
                "objectId": "cat_good_instruction",
                "Name": "\u05d4\u05d3\u05e8\u05db\u05d5\u05ea \u05d1\u05d8\u05d9\u05d7\u05d5\u05ea \u05d5\u05d4\u05d3\u05e8\u05db\u05d4 \u05d8\u05d5\u05d1\u05d4",
                "Code": "80025",
            },
            {"objectId": "cat_work_at_height", "Name": "\u05e2\u05d1\u05d5\u05d3\u05d4 \u05d1\u05d2\u05d5\u05d1\u05d4", "Code": "80015"},
            {"objectId": "cat_work_at_height_instructor_refresh", "Name": "\u05e8\u05e2\u05e0\u05d5\u05df \u05de\u05d3\u05e8\u05d9\u05da \u05d2\u05d5\u05d1\u05d4", "Code": "80018"},
            {"objectId": "cat_heavy_vehicle", "Name": "משאית משא כבד C", "Code": "80012"},
            {"objectId": "cat_public_transport", "Name": "קורס רכב ציבורי", "Code": "80013"},
        ]
        self.products = [
            {
                "objectId": "prod_forklift",
                "Name": "קורס מלגזה",
                "CatalogNumber": "80001",
                "Price": 1102,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_forklift"),
            },
            {
                "objectId": "prod_forklift_friday",
                "Name": "קורס מלגזה ימי שישי",
                "CatalogNumber": "80001-F",
                "Price": 1186,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_forklift"),
            },
            {
                "objectId": "prod_forklift_thai",
                "Name": "קורס מלגזה לתאילנדים",
                "CatalogNumber": "80001",
                "Price": 1391,
                "IsActive": True,
                "Category": None,
            },
            {
                "objectId": "prod_forklift_refresh",
                "Name": "רענון מלגזה",
                "CatalogNumber": "80003",
                "Price": 339,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_forklift_refresh"),
            },
            {
                "objectId": "prod_forklift_refresh_company",
                "Name": "רענון מלגזה לחברה",
                "CatalogNumber": "80003-C",
                "Price": 1700,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_forklift_refresh"),
            },
            {
                "objectId": "prod_tractor",
                "Name": "קורס טרקטור",
                "CatalogNumber": "80007",
                "Price": 2712,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_tractor"),
            },
            {
                "objectId": "prod_tachograph",
                "Name": "השתלמות טכוגרף דיגיטלי לקציני בטיחות",
                "CatalogNumber": "80049",
                "Price": 1000,
                "IsActive": False,
                "Category": pointer("ProductCategories", "cat_safety_officers_day"),
            },
            {
                "objectId": "prod_discount_only",
                "Name": "קורס הנחות",
                "CatalogNumber": "99999",
                "Price": 100,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_discount_only"),
            },
            {
                "objectId": "prod_safety_training",
                "Name": "\u05d4\u05d3\u05e8\u05db\u05ea \u05d1\u05d8\u05d9\u05d7\u05d5\u05ea",
                "CatalogNumber": "80025",
                "Price": 1995,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_good_instruction"),
            },
            {
                "objectId": "prod_good_instruction",
                "Name": "\u05e7\u05d5\u05e8\u05e1 \"\u05d4\u05d3\u05e8\u05db\u05d4 \u05d8\u05d5\u05d1\u05d4\"",
                "CatalogNumber": "80025",
                "Price": 2100,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_good_instruction"),
            },
            {
                "objectId": "prod_work_at_height",
                "Name": "\u05e2\u05d1\u05d5\u05e8 \u05d4\u05d3\u05e8\u05db\u05ea \u05e2\u05d1\u05d5\u05d3\u05d4 \u05d1\u05d2\u05d5\u05d1\u05d4",
                "CatalogNumber": "80015",
                "Price": 407,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_work_at_height"),
            },
            {
                "objectId": "prod_work_at_height_instructor_refresh",
                "Name": "\u05e2\u05d1\u05d5\u05e8 \u05d4\u05e9\u05ea\u05dc\u05de\u05d5\u05ea \u05e8\u05e2\u05e0\u05d5\u05df \u05dc\u05de\u05d3\u05e8\u05d9\u05db\u05d9 \u05e2\u05d1\u05d5\u05d3\u05d4 \u05d1\u05d2\u05d5\u05d1\u05d4",
                "CatalogNumber": "80018",
                "Price": 600,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_work_at_height_instructor_refresh"),
            },
            {
                "objectId": "prod_heavy_vehicle_theory",
                "Name": "עבור קורס משא כבד -",
                "CatalogNumber": "80012",
                "Price": 3729,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_heavy_vehicle"),
            },
            {
                "objectId": "prod_heavy_vehicle_practical",
                "Name": "חלק מעשי משא כבד",
                "CatalogNumber": "80012",
                "Price": 3846,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_heavy_vehicle"),
            },
            {
                "objectId": "prod_public_transport_course",
                "Name": "עבור קורס רכב ציבורי -",
                "CatalogNumber": "80013",
                "Price": 3729,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_public_transport"),
            },
            {
                "objectId": "prod_public_transport_misc",
                "Name": "רכב ציבורי -שונות",
                "CatalogNumber": "80013",
                "Price": 100,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_public_transport"),
            },
        ]
        self.payment_buttons = {
            "btn_forklift_full": {"objectId": "btn_forklift_full", "Name": "קורס מלגזה", "Title": "קורס מלגזה", "Active": True},
            "btn_forklift_deposit": {"objectId": "btn_forklift_deposit", "Name": "מקדמה קורס מלגזה", "Title": "מקדמה", "Active": True},
            "btn_forklift_friday": {"objectId": "btn_forklift_friday", "Name": "קורס מלגזה ימי שישי", "Title": "ימי שישי", "Active": True},
            "btn_refresh": {"objectId": "btn_refresh", "Name": "ריענון מלגזה בודדים", "Title": "ריענון מלגזה", "Active": True},
            "btn_tractor": {"objectId": "btn_tractor", "Name": "קורס טרקטור ישראלים", "Title": "קורס טרקטור", "Active": True},
            "btn_tractor_discount": {"objectId": "btn_tractor_discount", "Name": "10 אחוז הנחה לקורס טרקטור", "Active": True},
            "btn_tachograph": {
                "objectId": "btn_tachograph",
                "Name": "השתלמות טכוגרף דיגיטלי לקציני בטיחות",
                "Title": "",
                "Active": True,
            },
            "btn_discount_only": {"objectId": "btn_discount_only", "Name": "15 אחוז הנחה", "Active": True},
            "btn_work_at_height": {
                "objectId": "btn_work_at_height",
                "Name": "\u05d4\u05d3\u05e8\u05db\u05ea \u05e2\u05d1\u05d5\u05d3\u05d4 \u05d1\u05d2\u05d5\u05d1\u05d4",
                "Title": "\u05d3\u05e3 \u05ea\u05e9\u05dc\u05d5\u05dd \u05d4\u05d3\u05e8\u05db\u05ea \u05e2\u05d1\u05d5\u05d3\u05d4 \u05d1\u05d2\u05d5\u05d1\u05d4",
                "Active": True,
            },
            "btn_work_at_height_refresh": {
                "objectId": "btn_work_at_height_refresh",
                "Name": "\u05e8\u05d9\u05e2\u05e0\u05d5\u05df \u05e2\u05d1\u05d5\u05d3\u05d4 \u05d1\u05d2\u05d5\u05d1\u05d4",
                "Title": "\u05d4\u05d3\u05e8\u05db\u05ea \u05e8\u05d9\u05e2\u05e0\u05d5\u05df \u05e2\u05d1\u05d5\u05d3\u05d4 \u05d1\u05d2\u05d5\u05d1\u05d4",
                "Active": True,
            },
            "btn_work_at_height_instructor_refresh": {
                "objectId": "btn_work_at_height_instructor_refresh",
                "Name": "\u05d3\u05e3 \u05ea\u05e9\u05dc\u05d5\u05dd \u05e8\u05d9\u05e2\u05e0\u05d5\u05df \u05de\u05d3\u05e8\u05d9\u05db\u05d9 \u05e2\u05d1\u05d5\u05d3\u05d4 \u05d1\u05d2\u05d5\u05d1\u05d4",
                "Title": "\u05d3\u05e3 \u05ea\u05e9\u05dc\u05d5\u05dd \u05e8\u05d9\u05e2\u05e0\u05d5\u05df \u05de\u05d3\u05e8\u05d9\u05db\u05d9 \u05e2\u05d1\u05d5\u05d3\u05d4 \u05d1\u05d2\u05d5\u05d1\u05d4",
                "Active": True,
            },
            "btn_heavy_vehicle_theory": {
                "objectId": "btn_heavy_vehicle_theory",
                "Name": "קורס משא כבד עיוני",
                "Title": "דף תשלום קורס משא כבד עיוני",
                "Active": True,
            },
            "btn_public_transport_misc": {
                "objectId": "btn_public_transport_misc",
                "Name": "דף תשלום רכב ציבורי- שונות",
                "Title": "",
                "Active": True,
            },
        }
        self.rows = [
            row("row_f1", "btn_forklift_full", "prod_forklift", "קורס מלגזה מלא", 1102),
            row("row_f2", "btn_forklift_deposit", "prod_forklift", "מקדמה קורס מלגזה", 254),
            row("row_f3", "btn_forklift_friday", "prod_forklift_friday", "קורס מלגזה ימי שישי", 1186),
            row("row_r1", "btn_refresh", "prod_forklift_refresh", "ריענון מלגזה", 339),
            row("row_t1", "btn_tractor", "prod_tractor", "קורס טרקטור", 2712),
            row("row_t2", "btn_tractor_discount", "prod_tractor", "10 אחוז הנחה", 2000),
            row("row_tachograph", "btn_tachograph", "prod_tachograph", "השתלמות טכוגרף דיגיטלי לקציני בטיחות", 1000),
            row("row_d1", "btn_discount_only", "prod_discount_only", "הנחה", 100),
            row("row_h1", "btn_work_at_height", "prod_work_at_height", "\u05e2\u05d1\u05d5\u05e8 \u05d4\u05d3\u05e8\u05db\u05ea \u05e2\u05d1\u05d5\u05d3\u05d4 \u05d1\u05d2\u05d5\u05d1\u05d4", 407),
            row("row_h2", "btn_work_at_height_refresh", "prod_work_at_height", "\u05e2\u05d1\u05d5\u05e8 \u05d4\u05d3\u05e8\u05db\u05ea \u05e8\u05d9\u05e2\u05e0\u05d5\u05df \u05e2\u05d1\u05d5\u05d3\u05d4 \u05d1\u05d2\u05d5\u05d1\u05d4", 356),
            row(
                "row_h3",
                "btn_work_at_height_instructor_refresh",
                "prod_work_at_height_instructor_refresh",
                "\u05e8\u05d9\u05e2\u05e0\u05d5\u05df \u05de\u05d3\u05e8\u05d9\u05db\u05d9 \u05e2\u05d1\u05d5\u05d3\u05d4 \u05d1\u05d2\u05d5\u05d1\u05d4",
                500,
            ),
            {
                "objectId": "row_heavy_vehicle_theory",
                "PaymentBtnId": pointer("PaymentBtns", "btn_heavy_vehicle_theory"),
                "ProductId": None,
                "ProductDescription": "קורס רכב משא כבד עיוני",
                "Price": 3305.9,
            },
            row(
                "row_public_transport_misc",
                "btn_public_transport_misc",
                "prod_public_transport_misc",
                "רכב ציבורי -שונות",
                1000,
            ),
        ]

    async def _get_object(self, table_name: str, object_id: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if table_name == "ProductCategories":
            return next((item for item in self.categories if item["objectId"] == object_id), None)
        if table_name == "Products":
            product = next((item for item in self.products if item["objectId"] == object_id), None)
            return self._include_category(product) if product else None
        if table_name == "PaymentBtns":
            return self.payment_buttons.get(object_id)
        return None

    async def _get_class(self, table_name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        where = json.loads(params.get("where", "{}"))
        if table_name == "ProductCategories":
            rows = self.categories
            if "Code" in where:
                rows = [item for item in rows if item.get("Code") == where["Code"]]
            return rows
        if table_name == "Products":
            if "$or" in where:
                return [self._include_category(item) for item in self.products if row_matches_search(self._include_category(item), where["$or"])]
            category_id = where.get("Category", {}).get("objectId")
            return [
                self._include_category(item)
                for item in self.products
                if isinstance(item.get("Category"), dict) and item["Category"].get("objectId") == category_id
            ]
        if table_name == "PaymentBtnsRows":
            if "$or" in where:
                return [self._include_row(row_data) for row_data in self.rows if row_matches_search(self._include_row(row_data), where["$or"])]
            product_ids = {item["objectId"] for item in where.get("ProductId", {}).get("$in", [])}
            return [
                self._include_row(row_data)
                for row_data in self.rows
                if isinstance(row_data.get("ProductId"), dict) and row_data["ProductId"]["objectId"] in product_ids
            ]
        return []

    def _include_category(self, product: dict[str, Any]) -> dict[str, Any]:
        category_ref = product.get("Category") if isinstance(product.get("Category"), dict) else {}
        category_id = category_ref.get("objectId")
        category = next((item for item in self.categories if item["objectId"] == category_id), None)
        return {**product, "Category": category or product.get("Category")}

    def _include_row(self, row_data: dict[str, Any]) -> dict[str, Any]:
        product_ref = row_data.get("ProductId") if isinstance(row_data.get("ProductId"), dict) else None
        product = next((item for item in self.products if product_ref and item["objectId"] == product_ref["objectId"]), None)
        payment_button = self.payment_buttons[row_data["PaymentBtnId"]["objectId"]]
        return {
            **row_data,
            "ProductId": self._include_category(product) if product else None,
            "PaymentBtnId": payment_button,
        }


def row(row_id: str, button_id: str, product_id: str, description: str, price: int) -> dict[str, Any]:
    return {
        "objectId": row_id,
        "PaymentBtnId": pointer("PaymentBtns", button_id),
        "ProductId": pointer("Products", product_id),
        "ProductDescription": description,
        "Price": price,
    }


def row_matches_search(row_data: dict[str, Any], clauses: list[dict[str, Any]]) -> bool:
    for clause in clauses:
        for path, condition in clause.items():
            value = dotted_get(row_data, path)
            pattern = condition.get("$regex") if isinstance(condition, dict) else None
            if pattern and re.search(pattern, str(value or ""), re.IGNORECASE):
                return True
    return False


def dotted_get(data: dict[str, Any], path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def run(coro):
    return asyncio.run(coro)


def test_get_course_payment_links_returns_multiple_valid_links() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_payment_links(category_name="מלגזה"))

    assert result["found"] is True
    assert result["requires_user_choice"] is True
    assert result["required_customer_details"] == REQUIRED_CUSTOMER_DETAILS
    assert {link["payment_btn_id"] for link in result["payment_links"]} == {
        "btn_forklift_full",
        "btn_forklift_deposit",
        "btn_forklift_friday",
    }


def test_get_course_payment_links_can_resolve_by_product_id() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_payment_links(product_id="prod_forklift_refresh"))

    assert result["found"] is True
    assert result["requires_user_choice"] is False
    assert result["payment_links"][0]["payment_btn_id"] == "btn_refresh"
    assert result["payment_links"][0]["payment_url"].endswith("oid=btn_refresh")


def test_get_course_payment_links_searches_payment_api_columns() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_payment_links(category_name="טכוגרף"))

    assert result["found"] is True
    assert result["matched_by"] == "payment_rows_api_search"
    assert result["requires_user_choice"] is False
    assert result["payment_links"][0]["payment_btn_id"] == "btn_tachograph"
    assert result["payment_links"][0]["product"]["catalog_number"] == "80049"


def test_get_course_payment_links_falls_back_to_inactive_category_products_after_product_miss() -> None:
    service = PaymentLinkService(FakeMyBusiness())
    service.mybusiness.products.append(
        {
            "objectId": "prod_tachograph_general",
            "Name": "יום עיון לקציני בטיחות",
            "CatalogNumber": "80049",
            "Price": 400,
            "IsActive": True,
            "Category": pointer("ProductCategories", "cat_safety_officers_day"),
        }
    )

    result = run(
        service.get_course_payment_links(
            category_id="cat_safety_officers_day",
            category_code="80049",
            category_name="יום עיון לקציני בטיחות",
            product_id="prod_tachograph_general",
        )
    )

    assert result["found"] is True
    assert result["matched_by"] == "all_category_products_after_payment_miss"
    assert result["payment_links"][0]["payment_btn_id"] == "btn_tachograph"


def test_get_course_current_price_uses_payment_row_price() -> None:
    service = PaymentLinkService(FakeMyBusiness())
    service.mybusiness.products.append(
        {
            "objectId": "prod_tachograph_general",
            "Name": "יום עיון לקציני בטיחות",
            "CatalogNumber": "80049",
            "Price": 400,
            "IsActive": True,
            "Category": pointer("ProductCategories", "cat_safety_officers_day"),
        }
    )

    result = run(
        service.get_course_current_price(
            category_id="cat_safety_officers_day",
            category_code="80049",
            category_name="יום עיון לקציני בטיחות",
            product_id="prod_tachograph_general",
        )
    )

    assert result["found"] is True
    assert result["price_source"] == "PaymentBtnsRows.Price"
    assert result["prices"][0]["price"] == 1000
    assert "payment_url" not in result["prices"][0]


def test_get_course_current_price_falls_back_to_matching_product_price() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_current_price(category_name="\u05e7\u05d5\u05e8\u05e1 \u05d4\u05d3\u05e8\u05db\u05d4 \u05d8\u05d5\u05d1\u05d4"))

    assert result["found"] is True
    assert result["requires_user_choice"] is False
    assert result["price_source"] == "Products.Price fallback"
    assert result["matched_by"] == "products_api_search"
    assert result["prices"] == [
        {
            "price": 2100,
            "name": "\u05e7\u05d5\u05e8\u05e1 \"\u05d4\u05d3\u05e8\u05db\u05d4 \u05d8\u05d5\u05d1\u05d4\"",
            "title": "\u05e7\u05d5\u05e8\u05e1 \"\u05d4\u05d3\u05e8\u05db\u05d4 \u05d8\u05d5\u05d1\u05d4\"",
            "product_id": "prod_good_instruction",
            "product_name": "\u05e7\u05d5\u05e8\u05e1 \"\u05d4\u05d3\u05e8\u05db\u05d4 \u05d8\u05d5\u05d1\u05d4\"",
            "catalog_number": "80025",
            "category_name": "\u05d4\u05d3\u05e8\u05db\u05d5\u05ea \u05d1\u05d8\u05d9\u05d7\u05d5\u05ea \u05d5\u05d4\u05d3\u05e8\u05db\u05d4 \u05d8\u05d5\u05d1\u05d4",
            "category_code": "80025",
            "description_for_bot": "\u05e7\u05d5\u05e8\u05e1 \"\u05d4\u05d3\u05e8\u05db\u05d4 \u05d8\u05d5\u05d1\u05d4\" - 80025 - \u05de\u05d7\u05d9\u05e8 2100",
        }
    ]


def test_heavy_vehicle_current_price_combines_theory_link_and_practical_product() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_current_price(category_name="קורס משא כבד C"))

    assert result["found"] is True
    assert result["category"]["category_code"] == "80012"
    assert result["matched_by"] == "heavy_vehicle_category_and_component"
    assert result["price_source"] == "PaymentBtnsRows.Price + Products.Price fallback"
    assert [(price["price"], price["name"]) for price in result["prices"]] == [
        (3305.9, "קורס משא כבד עיוני"),
        (3846, "חלק מעשי משא כבד"),
    ]


def test_heavy_vehicle_practical_price_uses_exact_product_fallback() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_current_price(category_name="חלק מעשי משא כבד"))

    assert result["found"] is True
    assert result["requires_user_choice"] is False
    assert result["price_source"] == "Products.Price fallback"
    assert [(price["price"], price["name"]) for price in result["prices"]] == [(3846, "חלק מעשי משא כבד")]


def test_heavy_vehicle_category_id_still_uses_component_aware_prices() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_current_price(category_id="cat_heavy_vehicle"))

    assert result["matched_by"] == "heavy_vehicle_category_and_component"
    assert [price["price"] for price in result["prices"]] == [3305.9, 3846]


def test_heavy_vehicle_theory_price_does_not_return_catalog_or_deposit_price() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_current_price(category_name="משא כבד עיוני"))

    assert result["found"] is True
    assert result["requires_user_choice"] is False
    assert result["price_source"] == "PaymentBtnsRows.Price"
    assert [price["price"] for price in result["prices"]] == [3305.9]


def test_public_transport_price_ignores_miscellaneous_payment_amount() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_current_price(category_name="קורס רכב ציבורי"))

    assert result["found"] is True
    assert result["category"]["category_code"] == "80013"
    assert result["matched_by"] == "public_transport_canonical_product"
    assert result["price_source"] == "Products.Price fallback"
    assert [(price["price"], price["name"]) for price in result["prices"]] == [
        (3729, "עבור קורס רכב ציבורי -")
    ]


def test_heavy_vehicle_practical_never_returns_online_payment_link() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    by_name = run(
        service.get_course_payment_links(
            category_name="חלק מעשי משא כבד",
            payment_intent="PRACTICAL",
        )
    )
    by_product = run(service.get_course_payment_links(product_id="prod_heavy_vehicle_practical"))

    for result in (by_name, by_product):
        assert result["found"] is False
        assert result["requires_representative"] is True
        assert result["payment_links"] == []
        assert "directly to the driving instructor" in result["payment_guidance"]


def test_work_at_height_instructor_refresher_uses_specific_category_alias() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_current_price(category_name="ריענון מדריכי עבודה בגובה"))

    assert result["found"] is True
    assert result["category"]["category_code"] == "80018"
    assert {price["price"] for price in result["prices"]} == {500}


def test_past_year_payment_campaign_is_not_current() -> None:
    link = {
        "name": "ריענון מדריכי גובה 5.12.2022",
        "title": "תשלום",
        "description_for_bot": "מחיר ישן",
        "product": {"product_name": "ריענון מדריכי גובה"},
    }

    assert is_stale_dated_payment_link(link, current_year=2026)


def test_language_specific_price_does_not_fall_back_to_generic_forklift_price() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    thai = run(service.get_course_current_price(category_name="קורס מלגזה בתאית"))
    english = run(service.get_course_current_price(category_name="קורס מלגזה באנגלית"))

    assert thai["price_source"] == "Products.Price fallback"
    assert [price["price"] for price in thai["prices"]] == [1391]
    assert english["found"] is False
    assert english["prices"] == []


def test_company_forklift_refresher_price_is_separate_from_individual() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    company = run(service.get_course_current_price(category_name="רענון מלגזה לחברה"))
    individual = run(service.get_course_current_price(category_name="רענון מלגזה לבודד"))

    assert [price["price"] for price in company["prices"]] == [1700]
    assert [price["price"] for price in individual["prices"]] == [339]


def test_regular_work_at_height_query_uses_regular_course_price() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(
        service.get_course_current_price(
            category_name="\u05e7\u05d5\u05e8\u05e1 \u05d1\u05d2\u05d5\u05d1\u05d4 \u05e8\u05d0\u05e9\u05d5\u05e0\u05d9 \u05e8\u05d2\u05d9\u05dc"
        )
    )

    assert result["found"] is True
    assert result["requires_user_choice"] is False
    assert result["matched_by"] == "regular_work_at_height_alias"
    assert result["prices"][0]["price"] == 407
    assert result["prices"][0]["payment_btn_id"] == "btn_work_at_height"


def test_payment_intent_ranks_deposit_first_without_filtering() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_payment_links(category_name="מלגזה", payment_intent="DEPOSIT"))

    assert result["found"] is True
    assert result["payment_links"][0]["payment_btn_id"] == "btn_forklift_deposit"
    assert len(result["payment_links"]) == 3


def test_discount_links_are_restricted_and_not_returned_to_customer() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_payment_links(category_name="טרקטור"))

    assert result["found"] is True
    assert {link["payment_btn_id"] for link in result["payment_links"]} == {"btn_tractor"}
    assert result["restricted_links_summary"] == [
        {
            "payment_btn_id": "btn_tractor_discount",
            "name": "10 אחוז הנחה לקורס טרקטור",
            "restriction_reason": "DISCOUNT_LINK_NOT_ALLOWED",
        }
    ]


def test_only_discount_links_require_representative() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_payment_links(category_name="קורס הנחות"))

    assert result["found"] is False
    assert result["requires_representative"] is True
    assert result["payment_links"] == []
    assert result["restricted_links_summary"][0]["payment_btn_id"] == "btn_discount_only"


def test_missing_identifier_returns_missing() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_payment_links())

    assert result["found"] is False
    assert result["payment_links"] == []
