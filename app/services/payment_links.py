from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.services.course_catalog import (
    detect_course_language,
    resolve_course_category_code,
    text_matches_language,
)
from app.services.mybusiness import clean, json_dumps, normalize_text, pointer

TENANT_DOMAIN = "6a09b3ab-e66c-64a7-7dbc-06c797b56505.mbapps.co.il"
PAYMENT_URL_BASE = f"https://{TENANT_DOMAIN}/apps/mybooks/payment-btn-page?cls=PaymentBtns&oid="
REQUIRED_CUSTOMER_DETAILS = ["שם מלא", "מספר טלפון", "תעודת זהות", "מייל"]
PAYMENT_INTENTS = {"FULL", "DEPOSIT", "REFRESHER", "FRIDAY", "THEORY", "PRACTICAL", "EXAM", "GENERAL"}
WORK_AT_HEIGHT_CATEGORY_CODE = "80015"
WORK_AT_HEIGHT_CATEGORY_NAME = "\u05e2\u05d1\u05d5\u05d3\u05d4 \u05d1\u05d2\u05d5\u05d1\u05d4"
HEAVY_VEHICLE_CATEGORY_CODE = "80012"
PUBLIC_TRANSPORT_CATEGORY_CODE = "80013"
HEAVY_VEHICLE_PRACTICAL_PAYMENT_GUIDANCE = (
    "No online payment link is provided for the heavy-vehicle practical component. "
    "Payment is usually made directly to the driving instructor. State the current price and offer the relevant representative."
)
DISCOUNT_KEYWORDS = ("הנחה", "אחוז הנחה", "10 אחוז", "15 אחוז", "discount")
PAYMENT_SEARCH_STOPWORDS = {
    "course",
    "payment",
    "link",
    "קורס",
    "קורסי",
    "קורסים",
    "לקורס",
    "בקורס",
    "השתלמות",
    "הכשרה",
    "תשלום",
    "לתשלום",
    "הרשמה",
    "להרשמה",
    "של",
    "על",
    "עם",
    "את",
    "אל",
    "לקציני",
    "בטיחות",
}


class PaymentLinkService:
    def __init__(self, mybusiness: Any):
        self.mybusiness = mybusiness

    async def get_course_current_price(
        self,
        category_id: str | None = None,
        category_code: str | None = None,
        category_name: str | None = None,
        product_id: str | None = None,
    ) -> dict[str, Any]:
        heavy_vehicle = await self._resolve_heavy_vehicle_request(category_id, category_code, category_name)
        if heavy_vehicle:
            return await self._get_heavy_vehicle_current_price(heavy_vehicle, category_name)

        public_transport = await self._resolve_public_transport_request(category_id, category_code, category_name)
        if public_transport:
            return await self._get_public_transport_current_price(public_transport)

        payload = await self.get_course_payment_links(
            category_id=category_id,
            category_code=category_code,
            category_name=category_name,
            product_id=product_id,
        )
        payload_category = payload.get("category") or {}
        if not product_id and normalize_payment_text(payload_category.get("category_code")) == HEAVY_VEHICLE_CATEGORY_CODE:
            return await self._get_heavy_vehicle_current_price(payload_category, category_name)
        links = payload.get("payment_links") or []
        prices = current_price_options_from_links(links)
        price_source = "PaymentBtnsRows.Price"
        if not prices:
            product_fallback = await self._get_product_price_fallback(category_id, category_code, category_name, product_id)
            prices = product_fallback.get("prices") or []
            price_source = product_fallback.get("price_source") or price_source
            if prices:
                payload = {**payload, **{key: value for key, value in product_fallback.items() if key != "prices"}}
        return {
            "found": bool(prices),
            "requires_user_choice": len(prices) > 1,
            "requires_representative": payload.get("requires_representative", False),
            "category": payload.get("category"),
            "matched_by": payload.get("matched_by"),
            "prices": prices,
            "restricted_links_summary": payload.get("restricted_links_summary") or [],
            "reason": None if prices else payload.get("reason") or "No current payment price was found.",
            "price_source": price_source,
        }

    async def _resolve_public_transport_request(
        self,
        category_id: str | None,
        category_code: str | None,
        category_name: str | None,
    ) -> dict[str, Any] | None:
        if normalize_payment_text(category_code) == PUBLIC_TRANSPORT_CATEGORY_CODE or is_public_transport_text(category_name):
            result = await self._resolve_category(category_id, PUBLIC_TRANSPORT_CATEGORY_CODE, None)
            return result.get("category") if result.get("found") else None
        return None

    async def _get_public_transport_current_price(self, category: dict[str, Any]) -> dict[str, Any]:
        products = await self._get_products_for_category(category["category_id"])
        canonical_products = [
            product
            for product in products
            if product.get("IsActive") is not False and is_public_transport_course_product(product)
        ]
        prices = [
            format_product_price_option(product)
            for product in canonical_products
            if product.get("Price") is not None
        ]
        return {
            "found": bool(prices),
            "requires_user_choice": len(prices) > 1,
            "requires_representative": not prices,
            "category": category,
            "matched_by": "public_transport_canonical_product",
            "prices": prices,
            "restricted_links_summary": [],
            "reason": None if prices else "No canonical public-transport course price was found.",
            "price_source": "Products.Price fallback",
        }

    async def _resolve_heavy_vehicle_request(
        self,
        category_id: str | None,
        category_code: str | None,
        category_name: str | None,
    ) -> dict[str, Any] | None:
        if normalize_payment_text(category_code) == HEAVY_VEHICLE_CATEGORY_CODE or is_heavy_vehicle_text(category_name):
            result = await self._resolve_category(category_id, HEAVY_VEHICLE_CATEGORY_CODE, None)
            return result.get("category") if result.get("found") else None
        return None

    async def _get_heavy_vehicle_current_price(
        self,
        category: dict[str, Any],
        category_name: str | None,
    ) -> dict[str, Any]:
        component = heavy_vehicle_price_component(category_name)
        products = await self._get_products_for_category(category["category_id"])
        active_products = [product for product in products if product.get("IsActive") is not False]

        theory_prices: list[dict[str, Any]] = []
        if component != "practical":
            rows = await self._search_payment_rows_once("משא כבד")
            theory_links = []
            for row in rows:
                payment_btn = await self._resolve_payment_button(row)
                if not payment_btn or payment_btn.get("Active") is False:
                    continue
                product = row.get("ProductId") if isinstance(row.get("ProductId"), dict) else {}
                link = format_payment_link(payment_btn, row, product)
                if is_heavy_vehicle_theory_link(link) and not is_stale_dated_payment_link(link):
                    theory_links.append(link)
            theory_prices = current_price_options_from_links(theory_links)
            if not theory_prices:
                theory_prices = [
                    format_product_price_option(product)
                    for product in active_products
                    if heavy_vehicle_product_component(product) == "theory" and product.get("Price") is not None
                ]

        practical_prices = []
        if component != "theory":
            for product in active_products:
                if heavy_vehicle_product_component(product) != "practical" or product.get("Price") is None:
                    continue
                option = format_product_price_option(product)
                option["payment_link_available"] = False
                option["payment_guidance"] = HEAVY_VEHICLE_PRACTICAL_PAYMENT_GUIDANCE
                practical_prices.append(option)

        for price in theory_prices:
            price["payment_link_available"] = bool(price.get("payment_btn_id"))

        prices = dedupe_product_price_options([*theory_prices, *practical_prices])
        sources = []
        if theory_prices and any(price.get("payment_btn_id") for price in theory_prices):
            sources.append("PaymentBtnsRows.Price")
        if practical_prices or (theory_prices and not any(price.get("payment_btn_id") for price in theory_prices)):
            sources.append("Products.Price fallback")
        return {
            "found": bool(prices),
            "requires_user_choice": len(prices) > 1,
            "requires_representative": False,
            "category": category,
            "matched_by": "heavy_vehicle_category_and_component",
            "prices": prices,
            "restricted_links_summary": [],
            "reason": None if prices else "No current heavy vehicle price was found.",
            "price_source": " + ".join(sources) or "Products.Price fallback",
        }

    async def get_course_payment_links(
        self,
        category_id: str | None = None,
        category_code: str | None = None,
        category_name: str | None = None,
        product_id: str | None = None,
        payment_intent: str | None = None,
        include_restricted: bool = False,
    ) -> dict[str, Any]:
        if not any([category_id, category_code, category_name, product_id]):
            return {
                "found": False,
                "requires_user_choice": False,
                "requires_representative": False,
                "payment_links": [],
                "restricted_links_summary": [],
                "reason": "Missing category_id, category_code, category_name, or product_id.",
            }
        if not self.mybusiness.is_configured:
            return {
                "found": False,
                "requires_user_choice": False,
                "requires_representative": True,
                "payment_links": [],
                "restricted_links_summary": [],
                "reason": "MyBusiness API is not configured.",
            }

        intent = normalize_payment_intent(payment_intent)
        if (
            normalize_payment_text(category_code) == HEAVY_VEHICLE_CATEGORY_CODE or is_heavy_vehicle_text(category_name)
        ) and (intent == "PRACTICAL" or heavy_vehicle_price_component(category_name) == "practical"):
            category_result = await self._resolve_category(category_id, HEAVY_VEHICLE_CATEGORY_CODE, None)
            return heavy_vehicle_practical_no_link_payload(category_result.get("category"))

        product_result = await self._resolve_products(category_id, category_code, category_name, product_id)
        if not product_result.get("found"):
            return product_result

        category = product_result.get("category")
        products = product_result.get("products") or []
        if len(products) == 1 and heavy_vehicle_product_component(products[0]) == "practical":
            return heavy_vehicle_practical_no_link_payload(category)
        if not products:
            return {
                "found": False,
                "requires_user_choice": False,
                "requires_representative": False,
                "category": category,
                "payment_links": [],
                "restricted_links_summary": [],
                "reason": "No products were found for the requested course category.",
            }

        rows = product_result.get("payment_rows") or await self._get_payment_rows_for_products(
            [product["objectId"] for product in products if product.get("objectId")]
        )
        exact_product_search = product_result.get("matched_by") in {
            "language_specific_products_api_search",
            "company_forklift_refresher_products_api_search",
        }
        if not rows and not exact_product_search:
            fallback = await self._resolve_category_products_after_payment_miss(product_result)
            if fallback.get("found"):
                product_result = fallback
                category = product_result.get("category")
                products = product_result.get("products") or []
                rows = product_result.get("payment_rows") or []
        product_by_id = {product["objectId"]: product for product in products if product.get("objectId")}
        payment_links: list[dict[str, Any]] = []
        restricted_links: list[dict[str, Any]] = []
        seen_links: set[tuple[str | None, str | None, str | None]] = set()

        for row in rows:
            product = row.get("ProductId") if isinstance(row.get("ProductId"), dict) else None
            product_id_from_row = product.get("objectId") if product else None
            product = product_by_id.get(product_id_from_row) or product or {}
            payment_btn = await self._resolve_payment_button(row)
            if not payment_btn:
                continue
            if payment_btn.get("Active") is False:
                continue

            if is_discount_link(payment_btn, row, product):
                restricted_links.append(format_restricted_link(payment_btn))
                continue

            link = format_payment_link(payment_btn, row, product)
            if is_mismatched_course_payment_link(link, category):
                continue
            if is_stale_dated_payment_link(link):
                continue
            dedupe_key = (link.get("payment_btn_id"), link.get("product", {}).get("product_id"), str(link.get("row_price")))
            if dedupe_key in seen_links:
                continue
            seen_links.add(dedupe_key)
            payment_links.append(link)

        if is_regular_work_at_height_query(category_name):
            regular_links = [link for link in payment_links if is_regular_work_at_height_link(link)]
            if regular_links:
                payment_links = regular_links

        payment_links = rank_links_by_payment_intent(payment_links, intent)
        restricted_links = dedupe_restricted_links(restricted_links)

        if not payment_links and restricted_links:
            return {
                "found": False,
                "requires_user_choice": False,
                "requires_representative": True,
                "category": category,
                "payment_links": [],
                "restricted_links_summary": restricted_links,
                "reason": "Only discount payment links were found. The bot is not authorized to provide discount links.",
                "required_customer_details": REQUIRED_CUSTOMER_DETAILS,
            }

        return {
            "found": bool(payment_links),
            "requires_user_choice": len(payment_links) > 1,
            "requires_representative": False,
            "category": category,
            "matched_by": product_result.get("matched_by"),
            "matched_categories": product_result.get("matched_categories"),
            "products_count": len(products),
            "payment_links": payment_links,
            "restricted_links_summary": restricted_links,
            "required_customer_details": REQUIRED_CUSTOMER_DETAILS,
            "reason": None if payment_links else "No payment links were found for the requested course category or product.",
            "include_restricted": include_restricted,
        }

    def allowed_payment_urls(self) -> set[str]:
        # Dynamic payment links are validated by domain/path in the output guardrail.
        return set()

    async def _resolve_products(
        self,
        category_id: str | None,
        category_code: str | None,
        category_name: str | None,
        product_id: str | None,
    ) -> dict[str, Any]:
        if product_id:
            product = await self.mybusiness._get_object("Products", product_id, {"include": "Category"})
            if not product:
                return not_found("Product was not found.")
            category = map_category_ref(product.get("Category"))
            return {"found": True, "matched_by": "product_id", "category": category, "products": [product]}

        if is_company_forklift_refresher_query(category_name) and not category_id and not category_code:
            category_result = await self._resolve_category(None, "80003", None)
            category = category_result.get("category") or {}
            products = await self._get_products_for_category(category.get("category_id")) if category.get("category_id") else []
            company_products = [product for product in products if is_company_forklift_refresher_product(product)]
            if company_products:
                return {
                    "found": True,
                    "matched_by": "company_forklift_refresher_products_api_search",
                    "search": category_name,
                    "category": category,
                    "matched_categories": [category],
                    "products": company_products,
                }
            return not_found("No approved company forklift refresher product was found.")

        language = detect_course_language(category_name)
        if language and language != "עברית" and category_name and not category_id and not category_code:
            products = await self._search_products_once(category_name)
            language_products = [product for product in products if text_matches_language(product.get("Name"), language)]
            if language_products:
                categories = categories_from_products(language_products)
                return {
                    "found": True,
                    "matched_by": "language_specific_products_api_search",
                    "search": category_name,
                    "category": categories[0] if len(categories) == 1 else None,
                    "matched_categories": categories,
                    "products": language_products,
                }
            return not_found(f"No approved {language} product was found for this course.")

        category_result = await self._resolve_category(category_id, category_code, category_name)
        if not category_result.get("found"):
            if category_name and not category_id and not category_code and not category_result.get("ambiguous_category"):
                payment_rows = await self._search_payment_rows_once(category_name)
                if payment_rows:
                    products = products_from_payment_rows(payment_rows)
                    categories = categories_from_products(products)
                    return {
                        "found": True,
                        "matched_by": "payment_rows_api_search",
                        "search": category_name,
                        "category": categories[0] if len(categories) == 1 else None,
                        "matched_categories": categories,
                        "products": products,
                        "payment_rows": payment_rows,
                    }
            return category_result

        category = category_result["category"]
        products = await self._get_products_for_category(category["category_id"])
        active_or_unspecified = [product for product in products if product.get("IsActive") is not False]
        return {
            "found": True,
            "category": category,
            "products": active_or_unspecified or products,
            "all_category_products": products,
            "matched_by": category_result.get("matched_by"),
        }

    async def _resolve_category_products_after_payment_miss(self, product_result: dict[str, Any]) -> dict[str, Any]:
        category = product_result.get("category") or {}
        category_id = category.get("category_id")
        if not category_id:
            return {"found": False}

        products = product_result.get("all_category_products") or await self._get_products_for_category(category_id)
        rows = await self._get_payment_rows_for_products([product["objectId"] for product in products if product.get("objectId")])
        if not rows:
            return {"found": False}
        return {
            "found": True,
            "matched_by": "all_category_products_after_payment_miss",
            "category": category,
            "products": products,
            "payment_rows": rows,
        }

    async def _resolve_category(
        self,
        category_id: str | None,
        category_code: str | None,
        category_name: str | None,
    ) -> dict[str, Any]:
        if category_id:
            row = await self.mybusiness._get_object("ProductCategories", category_id)
            if not row:
                return not_found("Course category was not found.")
            return {"found": True, "category": map_category_ref(row)}

        if category_code:
            rows = await self.mybusiness._get_class(
                "ProductCategories",
                {
                    "where": json_dumps({"Code": str(category_code)}),
                    "limit": 100,
                    "keys": "objectId,Name,Code,createdAt,updatedAt",
                },
            )
            return resolve_category_matches(rows)

        rows = await self.mybusiness._get_class(
            "ProductCategories",
            {
                "limit": 1000,
                "order": "Name",
                "keys": "objectId,Name,Code,createdAt,updatedAt",
            },
        )
        query = normalize_payment_text(category_name)
        alias_code = resolve_course_category_code(category_name)
        if alias_code:
            aliased = [row for row in rows if normalize_payment_text(row.get("Code")) == alias_code]
            if aliased:
                return {
                    **resolve_category_matches(aliased),
                    "matched_by": "course_category_alias",
                }
        if is_regular_work_at_height_query(category_name):
            work_at_height = [
                row
                for row in rows
                if normalize_payment_text(row.get("Code")) == WORK_AT_HEIGHT_CATEGORY_CODE
                or normalize_payment_text(row.get("Name")) == WORK_AT_HEIGHT_CATEGORY_NAME
            ]
            if work_at_height:
                return {
                    **resolve_category_matches(work_at_height),
                    "matched_by": "regular_work_at_height_alias",
                }

        exact = [row for row in rows if normalize_payment_text(row.get("Name")) == query]
        if exact:
            return resolve_category_matches(exact)

        partial = [row for row in rows if query and query in normalize_payment_text(row.get("Name"))]
        return resolve_category_matches(partial)

    async def _search_payment_rows_once(self, search: str) -> list[dict[str, Any]]:
        terms = payment_search_terms(search)
        if not terms:
            return []
        return await self.mybusiness._get_class(
            "PaymentBtnsRows",
            {
                "where": json_dumps(payment_rows_search_where(terms)),
                "limit": 1000,
                "include": "PaymentBtnId,ProductId,ProductId.Category",
                "keys": "objectId,PaymentBtnId,ProductId,ProductDescription,Price,MinQuantity,MaxQuantity,CurrencyRate,createdAt,updatedAt",
            },
        )

    async def _get_products_for_category(self, category_id: str) -> list[dict[str, Any]]:
        return await self.mybusiness._get_class(
            "Products",
            {
                "where": json_dumps({"Category": pointer("ProductCategories", category_id)}),
                "limit": 1000,
                "include": "Category",
                "keys": "objectId,Name,CatalogNumber,Price,IsActive,Category,createdAt,updatedAt",
            },
        )

    async def _get_payment_rows_for_products(self, product_ids: list[str]) -> list[dict[str, Any]]:
        if not product_ids:
            return []
        return await self.mybusiness._get_class(
            "PaymentBtnsRows",
            {
                "where": json_dumps({"ProductId": {"$in": [pointer("Products", product_id) for product_id in product_ids]}}),
                "limit": 1000,
                "include": "PaymentBtnId,ProductId,ProductId.Category",
                "keys": "objectId,PaymentBtnId,ProductId,ProductDescription,Price,MinQuantity,MaxQuantity,CurrencyRate,createdAt,updatedAt",
            },
        )

    async def _resolve_payment_button(self, row: dict[str, Any]) -> dict[str, Any] | None:
        payment_btn = row.get("PaymentBtnId")
        if isinstance(payment_btn, dict) and payment_btn.get("objectId") and (payment_btn.get("Name") or payment_btn.get("Title")):
            return payment_btn
        payment_btn_id = payment_btn.get("objectId") if isinstance(payment_btn, dict) else None
        if not payment_btn_id:
            return None
        return await self.mybusiness._get_object(
            "PaymentBtns",
            payment_btn_id,
            {
                "keys": (
                    "objectId,Name,Title,TopParagraph,Footer,Link,Active,OrdersCount,IsPayments,"
                    "AllowedPayments,NoVat,VatPercent,RoundTotal,createdAt,updatedAt"
                )
            },
        )

    async def _get_product_price_fallback(
        self,
        category_id: str | None,
        category_code: str | None,
        category_name: str | None,
        product_id: str | None,
    ) -> dict[str, Any]:
        product_result = await self._resolve_products_for_price_fallback(category_id, category_code, category_name, product_id)
        products = product_result.get("products") or []
        prices = current_price_options_from_products(products, category_name)
        return {
            "found": bool(prices),
            "requires_user_choice": len(prices) > 1,
            "requires_representative": False,
            "category": product_result.get("category"),
            "matched_by": product_result.get("matched_by") or "products_price_fallback",
            "matched_categories": product_result.get("matched_categories"),
            "products_count": len(products),
            "prices": prices,
            "reason": None if prices else product_result.get("reason") or "No product price fallback was found.",
            "price_source": "Products.Price fallback",
        }

    async def _resolve_products_for_price_fallback(
        self,
        category_id: str | None,
        category_code: str | None,
        category_name: str | None,
        product_id: str | None,
    ) -> dict[str, Any]:
        product_result = await self._resolve_products(category_id, category_code, category_name, product_id)
        if product_result.get("found"):
            return product_result
        if detect_course_language(category_name) or is_company_forklift_refresher_query(category_name):
            return product_result
        if not category_name or category_id or category_code or product_id:
            return product_result

        products = await self._search_products_once(category_name)
        if not products:
            return product_result
        categories = categories_from_products(products)
        return {
            "found": True,
            "matched_by": "products_api_search",
            "search": category_name,
            "category": categories[0] if len(categories) == 1 else None,
            "matched_categories": categories,
            "products": products,
        }

    async def _search_products_once(self, search: str) -> list[dict[str, Any]]:
        terms = payment_search_terms(search)
        if not terms:
            return []
        fields = ["Name", "CatalogNumber", "Category.Name", "Category.Code"]
        clauses = []
        for term in terms:
            regex = {"$regex": re.escape(term), "$options": "i"}
            clauses.extend({field: regex} for field in fields)
        return await self.mybusiness._get_class(
            "Products",
            {
                "where": json_dumps({"$or": clauses}),
                "limit": 1000,
                "include": "Category",
                "keys": "objectId,Name,CatalogNumber,Price,IsActive,Category,createdAt,updatedAt",
            },
        )


def resolve_category_matches(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) == 1:
        return {"found": True, "category": map_category_ref(rows[0])}
    if len(rows) > 1:
        return {
            "found": False,
            "ambiguous_category": True,
            "matches_count": len(rows),
            "matches": [map_category_ref(row) for row in rows],
            "payment_links": [],
            "restricted_links_summary": [],
        }
    return not_found("No matching course category found.")


def not_found(reason: str) -> dict[str, Any]:
    return {
        "found": False,
        "requires_user_choice": False,
        "requires_representative": False,
        "payment_links": [],
        "restricted_links_summary": [],
        "reason": reason,
        "required_customer_details": REQUIRED_CUSTOMER_DETAILS,
    }


def map_category_ref(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    return {
        "category_id": row.get("objectId"),
        "category_name": clean(row.get("Name")),
        "category_code": clean(row.get("Code")),
        "created_at": row.get("createdAt"),
        "updated_at": row.get("updatedAt"),
    }


def payment_search_terms(search: Any) -> list[str]:
    query = normalize_payment_text(search)
    if not query:
        return []
    terms = [query]
    for token in query.split():
        if len(token) >= 3 and token not in PAYMENT_SEARCH_STOPWORDS and token not in terms:
            terms.append(token)
    return terms


def payment_rows_search_where(terms: list[str]) -> dict[str, Any]:
    fields = [
        "ProductDescription",
        "ProductId.Name",
        "ProductId.CatalogNumber",
        "ProductId.Category.Name",
        "ProductId.Category.Code",
        "PaymentBtnId.Name",
        "PaymentBtnId.Title",
        "PaymentBtnId.TopParagraph",
        "PaymentBtnId.Footer",
    ]
    clauses = []
    for term in terms:
        regex = {"$regex": re.escape(term), "$options": "i"}
        clauses.extend({field: regex} for field in fields)
    return {
        "$or": clauses
    }


def products_from_payment_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    products: dict[str, dict[str, Any]] = {}
    for row in rows:
        product = row.get("ProductId") if isinstance(row.get("ProductId"), dict) else None
        if product and product.get("objectId"):
            products[product["objectId"]] = product
    return list(products.values())


def categories_from_products(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    categories: dict[str, dict[str, Any]] = {}
    for product in products:
        category = map_category_ref(product.get("Category"))
        if category and category.get("category_id"):
            categories[category["category_id"]] = category
    return list(categories.values())


def format_payment_link(payment_btn: dict[str, Any], row: dict[str, Any], product: dict[str, Any]) -> dict[str, Any]:
    payment_btn_id = payment_btn.get("objectId")
    name = clean(payment_btn.get("Name")) or clean(payment_btn.get("Title"))
    title = clean(payment_btn.get("Title"))
    row_price = row.get("Price")
    product_name = clean(product.get("Name"))
    return {
        "payment_btn_id": payment_btn_id,
        "name": name,
        "title": title,
        "active": payment_btn.get("Active"),
        "row_price": row_price,
        "product": {
            "product_id": product.get("objectId"),
            "product_name": product_name,
            "catalog_number": clean(product.get("CatalogNumber")),
            "product_price": product.get("Price"),
            "product_is_active": product.get("IsActive"),
        },
        "is_payments": payment_btn.get("IsPayments"),
        "allowed_payments": payment_btn.get("AllowedPayments"),
        "orders_count": payment_btn.get("OrdersCount"),
        "payment_url": build_payment_url(payment_btn_id, payment_btn.get("Link")),
        "description_for_bot": build_description(name, title, row.get("ProductDescription"), product_name, row_price),
    }


def current_price_options_from_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, str | None]] = set()
    prices: list[dict[str, Any]] = []
    for link in links:
        row_price = link.get("row_price")
        product = link.get("product") or {}
        key = (row_price, link.get("payment_btn_id"))
        if key in seen:
            continue
        seen.add(key)
        prices.append(
            {
                "price": row_price,
                "payment_btn_id": link.get("payment_btn_id"),
                "name": link.get("name"),
                "title": link.get("title"),
                "product_name": product.get("product_name"),
                "catalog_number": product.get("catalog_number"),
                "description_for_bot": link.get("description_for_bot"),
            }
        )
    return prices


def is_stale_dated_payment_link(link: dict[str, Any], current_year: int | None = None) -> bool:
    """Reject links whose label explicitly identifies a past-year campaign."""
    year = current_year or datetime.now(UTC).year
    product = link.get("product") or {}
    text = " ".join(
        str(value or "")
        for value in (
            link.get("name"),
            link.get("title"),
            link.get("description_for_bot"),
            product.get("product_name"),
        )
    )
    embedded_years = {int(match) for match in re.findall(r"\b20\d{2}\b", text)}
    return bool(embedded_years and max(embedded_years) < year)


def is_mismatched_course_payment_link(link: dict[str, Any], category: dict[str, Any] | None) -> bool:
    """Reject a payment button that explicitly names a different course family."""
    category = category or {}
    category_code = normalize_payment_text(category.get("category_code"))
    category_name = normalize_payment_text(category.get("category_name"))
    product = link.get("product") or {}
    link_text = normalize_payment_text(
        " ".join(
            str(value or "")
            for value in (
                link.get("name"),
                link.get("title"),
                link.get("description_for_bot"),
                product.get("product_name"),
            )
        )
    )
    is_heavy_vehicle = category_code == "80012" or "משא כבד" in category_name
    return bool(is_heavy_vehicle and "עגורן" in link_text)


def is_heavy_vehicle_text(value: Any) -> bool:
    return resolve_course_category_code(value) == HEAVY_VEHICLE_CATEGORY_CODE


def is_public_transport_text(value: Any) -> bool:
    return resolve_course_category_code(value) == PUBLIC_TRANSPORT_CATEGORY_CODE


def is_public_transport_course_product(product: dict[str, Any]) -> bool:
    text = normalize_payment_text(product.get("Name"))
    excluded = ("שונות", "השלמת", "מקדמה", "דמי רישום", "ספר", "אגרה")
    return "רכב ציבורי" in text and "קורס" in text and not any(marker in text for marker in excluded)


def heavy_vehicle_price_component(value: Any) -> str | None:
    text = normalize_payment_text(value)
    if "מעשי" in text:
        return "practical"
    if "עיוני" in text:
        return "theory"
    return None


def heavy_vehicle_product_component(product: dict[str, Any]) -> str | None:
    text = normalize_payment_text(product.get("Name"))
    if not any(marker in text for marker in ("משא כבד", "משאית משא כבד", "רכב משא כבד")):
        return None
    if "מעשי" in text:
        return "practical"
    excluded = ("מקדמה", "דמי רישום", "ספר", "אגרה")
    if not any(marker in text for marker in excluded):
        return "theory"
    return None


def is_heavy_vehicle_theory_link(link: dict[str, Any]) -> bool:
    product = link.get("product") or {}
    text = normalize_payment_text(
        " ".join(
            str(value or "")
            for value in (
                link.get("name"),
                link.get("title"),
                link.get("description_for_bot"),
                product.get("product_name"),
            )
        )
    )
    is_heavy = any(marker in text for marker in ("משא כבד", "משאית משא כבד", "רכב משא כבד"))
    excluded = ("מקדמה", "דמי רישום", "ספר", "מעשי", "עגורן")
    return bool(is_heavy and "עיוני" in text and not any(marker in text for marker in excluded))


def heavy_vehicle_practical_no_link_payload(category: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "found": False,
        "requires_user_choice": False,
        "requires_representative": True,
        "category": category,
        "payment_links": [],
        "restricted_links_summary": [],
        "required_customer_details": REQUIRED_CUSTOMER_DETAILS,
        "reason": "Heavy-vehicle practical driving is paid directly to the driving instructor; no online payment link is available.",
        "payment_guidance": HEAVY_VEHICLE_PRACTICAL_PAYMENT_GUIDANCE,
    }


def is_company_forklift_refresher_query(value: Any) -> bool:
    text = normalize_payment_text(value).replace("ריענון", "רענון")
    return bool(
        "מלגזה" in text
        and "רענון" in text
        and any(marker in text for marker in ("חברה", "חברות", "קבוצה", "ארגון", "מפעל"))
    )


def is_company_forklift_refresher_product(product: dict[str, Any]) -> bool:
    text = normalize_payment_text(product.get("Name")).replace("ריענון", "רענון")
    individual_markers = ("תלמיד", "בודד", "יחיד")
    company_markers = ("שנתי", "חברה", "חברות", "קבוצה", "ארגון", "מפעל")
    return bool(
        "מלגזה" in text
        and "רענון" in text
        and any(marker in text for marker in company_markers)
        and not any(marker in text for marker in individual_markers)
    )


def current_price_options_from_products(products: list[dict[str, Any]], search: Any = None) -> list[dict[str, Any]]:
    candidates = [product for product in products if product.get("IsActive") is not False and product.get("Price") is not None]
    if not candidates:
        return []

    query = normalize_payment_text(search)
    if query:
        scored = [
            (product_price_match_score(product, query), product)
            for product in candidates
            if is_sufficient_product_price_match(product, query)
        ]
        if not scored:
            return []
        max_score = max(score for score, _product in scored)
        if max_score <= 0:
            return []
        candidates = [product for score, product in scored if score == max_score]

    return dedupe_product_price_options([format_product_price_option(product) for product in candidates])


def product_price_match_score(product: dict[str, Any], query: str) -> int:
    terms = payment_search_terms(query)
    product_name = normalize_payment_text(product.get("Name"))
    catalog_number = normalize_payment_text(product.get("CatalogNumber"))
    category = product.get("Category") if isinstance(product.get("Category"), dict) else {}
    category_name = normalize_payment_text(category.get("Name"))
    category_code = normalize_payment_text(category.get("Code"))

    score = 0
    if query and query in product_name:
        score += 100
    if query and (query in category_name or query in catalog_number or query in category_code):
        score += 20

    for term in terms:
        if term in product_name:
            score += 20
        if term in catalog_number or term in category_code:
            score += 5
        if term in category_name:
            score += 3
    return score


def is_sufficient_product_price_match(product: dict[str, Any], query: str) -> bool:
    """Reject weak fallback matches caused by a single generic shared word."""
    product_name = normalize_payment_text(product.get("Name"))
    catalog_number = normalize_payment_text(product.get("CatalogNumber"))
    category = product.get("Category") if isinstance(product.get("Category"), dict) else {}
    category_name = normalize_payment_text(category.get("Name"))
    category_code = normalize_payment_text(category.get("Code"))
    searchable = f"{product_name} {catalog_number} {category_name} {category_code}"

    if query in searchable:
        return True

    language = detect_course_language(query)
    if language and text_matches_language(product_name, language):
        return True

    terms = [term for term in payment_search_terms(query)[1:] if len(term) >= 3]
    if not terms:
        return False
    matched = sum(1 for term in terms if term in searchable)
    required = 1 if len(terms) == 1 else 2
    return matched >= required


def format_product_price_option(product: dict[str, Any]) -> dict[str, Any]:
    category = product.get("Category") if isinstance(product.get("Category"), dict) else {}
    product_name = clean(product.get("Name"))
    catalog_number = clean(product.get("CatalogNumber"))
    price = product.get("Price")
    description = " - ".join(str(item) for item in [product_name, catalog_number, f"מחיר {price}"] if item)
    return {
        "price": price,
        "name": product_name,
        "title": product_name,
        "product_id": product.get("objectId"),
        "product_name": product_name,
        "catalog_number": catalog_number,
        "category_name": clean(category.get("Name")) if isinstance(category, dict) else None,
        "category_code": clean(category.get("Code")) if isinstance(category, dict) else None,
        "description_for_bot": description,
    }


def dedupe_product_price_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, str | None]] = set()
    unique = []
    for option in options:
        key = (option.get("price"), option.get("product_id"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(option)
    return unique


def build_payment_url(payment_btn_id: str | None, link_field: Any) -> str | None:
    link = str(link_field or "").strip()
    if link:
        return link
    if not payment_btn_id:
        return None
    return f"{PAYMENT_URL_BASE}{payment_btn_id}"


def is_approved_dynamic_payment_url(url: str) -> bool:
    cleaned = str(url or "").rstrip(".,;:!?")
    parsed = urlparse(cleaned)
    query = parse_qs(parsed.query, keep_blank_values=True)
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == TENANT_DOMAIN
        and parsed.username is None
        and parsed.password is None
        and parsed.port in (None, 443)
        and parsed.path == "/apps/mybooks/payment-btn-page"
        and query.get("cls") == ["PaymentBtns"]
        and len(query.get("oid", [])) == 1
        and re.fullmatch(r"[A-Za-z0-9_-]+", query["oid"][0] or "")
    )


def build_description(name: Any, title: Any, row_description: Any, product_name: Any, price: Any) -> str:
    parts = [clean(item) for item in (name, title, row_description, product_name) if clean(item)]
    text = " - ".join(dict.fromkeys(str(item) for item in parts))
    if price is not None:
        text = f"{text} - מחיר {price}" if text else f"מחיר {price}"
    return text


def is_discount_link(payment_btn: dict[str, Any], row: dict[str, Any], product: dict[str, Any]) -> bool:
    fields = [
        payment_btn.get("Name"),
        payment_btn.get("Title"),
        payment_btn.get("TopParagraph"),
        payment_btn.get("Footer"),
        row.get("ProductDescription"),
        product.get("Name"),
    ]
    text = normalize_payment_text(" ".join(str(clean(field) or "") for field in fields))
    return any(keyword in text for keyword in DISCOUNT_KEYWORDS)


def format_restricted_link(payment_btn: dict[str, Any]) -> dict[str, str | None]:
    return {
        "payment_btn_id": payment_btn.get("objectId"),
        "name": clean(payment_btn.get("Name")) or clean(payment_btn.get("Title")),
        "restriction_reason": "DISCOUNT_LINK_NOT_ALLOWED",
    }


def dedupe_restricted_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    unique = []
    for link in links:
        key = link.get("payment_btn_id") or link.get("name")
        if key in seen:
            continue
        seen.add(key)
        unique.append(link)
    return unique


def normalize_payment_intent(value: str | None) -> str:
    intent = str(value or "GENERAL").strip().upper()
    return intent if intent in PAYMENT_INTENTS else "GENERAL"


def rank_links_by_payment_intent(links: list[dict[str, Any]], intent: str) -> list[dict[str, Any]]:
    if intent == "GENERAL":
        return links
    return sorted(links, key=lambda link: intent_score(link, intent), reverse=True)


def is_regular_work_at_height_query(value: Any) -> bool:
    text = normalize_payment_text(value)
    if not text:
        return False

    has_height = WORK_AT_HEIGHT_CATEGORY_NAME in text or "\u05d1\u05d2\u05d5\u05d1\u05d4" in text or "\u05d2\u05d5\u05d1\u05d4" in text
    if not has_height:
        return False

    excluded = (
        "\u05e8\u05e2\u05e0\u05d5\u05df",
        "\u05e8\u05d9\u05e2\u05e0\u05d5\u05df",
        "\u05de\u05d3\u05e8\u05d9\u05da",
        "\u05de\u05d3\u05e8\u05d9\u05db\u05d9",
        "\u05de\u05d3\u05e8\u05d9\u05db\u05d9\u05dd",
        "\u05e6\u05d9\u05d5\u05d3",
        "\u05d7\u05d1\u05e8\u05d4",
    )
    if any(term in text for term in excluded):
        return False

    regular_markers = (
        "\u05e7\u05d5\u05e8\u05e1",
        "\u05d4\u05d3\u05e8\u05db\u05d4",
        "\u05e8\u05d0\u05e9\u05d5\u05e0\u05d9",
        "\u05e8\u05d2\u05d9\u05dc",
        WORK_AT_HEIGHT_CATEGORY_NAME,
    )
    return any(term in text for term in regular_markers)


def is_regular_work_at_height_link(link: dict[str, Any]) -> bool:
    product = link.get("product") or {}
    text = normalize_payment_text(
        " ".join(
            str(item or "")
            for item in [
                link.get("name"),
                link.get("title"),
                link.get("description_for_bot"),
                product.get("product_name"),
                product.get("catalog_number"),
            ]
        )
    )
    if WORK_AT_HEIGHT_CATEGORY_CODE not in text and WORK_AT_HEIGHT_CATEGORY_NAME not in text and "\u05d1\u05d2\u05d5\u05d1\u05d4" not in text:
        return False
    excluded = (
        "\u05e8\u05e2\u05e0\u05d5\u05df",
        "\u05e8\u05d9\u05e2\u05e0\u05d5\u05df",
        "\u05de\u05d3\u05e8\u05d9\u05da",
        "\u05de\u05d3\u05e8\u05d9\u05db\u05d9",
        "\u05de\u05d3\u05e8\u05d9\u05db\u05d9\u05dd",
        "\u05e6\u05d9\u05d5\u05d3",
    )
    return not any(term in text for term in excluded)


def intent_score(link: dict[str, Any], intent: str) -> int:
    text = normalize_payment_text(
        " ".join(
            str(item or "")
            for item in [
                link.get("name"),
                link.get("title"),
                link.get("description_for_bot"),
                link.get("product", {}).get("product_name"),
            ]
        )
    )
    positive = {
        "DEPOSIT": ("מקדמה", "דמי רישום"),
        "REFRESHER": ("רענון", "ריענון"),
        "FRIDAY": ("שישי", "יום ו"),
        "THEORY": ("תאוריה", "תיאוריה", "עיוני"),
        "PRACTICAL": ("מעשי",),
        "EXAM": ("בחינה", "בחינות", "חידוש"),
    }
    negative_for_full = ("מקדמה", "רענון", "ריענון", "הנחה", "תאוריה", "תיאוריה", "מעשי")
    if intent == "FULL":
        return 10 - sum(1 for token in negative_for_full if token in text)
    return sum(5 for token in positive.get(intent, ()) if token in text)


def normalize_payment_text(value: Any) -> str:
    text = normalize_text(value)
    text = text.replace("ריענון", "רענון")
    text = text.replace('חומ"ס', "חומס").replace("חומ״ס", "חומס").replace("חו מס", "חומס")
    text = re.sub(r"[\"'״׳`´]+", "", text)
    text = re.sub(r"[^\w\u0590-\u05ff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()
