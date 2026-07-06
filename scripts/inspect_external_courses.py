from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


KNOWN_EXTERNAL_COURSE_NAME = "קורס טרקטור נען"
INSPECT_LIMIT = 100
RECENT_COURSES_LIMIT = 30
SEARCH_WORDS = (
    "חיצוני",
    "חיצונית",
    "external",
    "outside",
    "outsourced",
    "third",
    "isexternal",
    "externalcourse",
)


def normalize(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "")


def looks_relevant_key(key: str) -> bool:
    normalized = normalize(key)
    return any(normalize(word) in normalized for word in SEARCH_WORDS)


def looks_relevant_value(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        normalized = normalize(value)
        return value.strip() in {"כן", "לא"} or any(normalize(word) in normalized for word in SEARCH_WORDS)
    return False


def walk(obj: Any, path: str = "") -> list[tuple[str, Any]]:
    matches = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path else key
            if looks_relevant_key(key) or looks_relevant_value(value):
                matches.append((child_path, value))
            matches.extend(walk(value, child_path))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            matches.extend(walk(value, f"{path}[{index}]"))
    return matches


def pretty(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


async def get_json(client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None) -> Any:
    response = await client.get(path, params=params or {})
    response.raise_for_status()
    return response.json()


async def inspect_schema(client: httpx.AsyncClient) -> None:
    print("=== SCHEMA: Courses ===")
    try:
        schema = await get_json(client, "/schemas/Courses")
    except Exception as exc:  # noqa: BLE001 - diagnostic script should continue to row inspection.
        print(f"Schema fetch failed: {type(exc).__name__}: {exc}")
        return

    fields = schema.get("fields", {})
    matching_fields = {
        field_name: field_def
        for field_name, field_def in fields.items()
        if looks_relevant_key(field_name) or looks_relevant_value(field_def)
    }
    if not matching_fields:
        print("No schema fields with external-course-looking names were found.")
        print("All field names:")
        print(", ".join(sorted(fields.keys())))
        return

    for field_name, field_def in sorted(matching_fields.items()):
        print(f"{field_name}: {pretty(field_def)}")


async def fetch_courses(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    where = json.dumps(
        {"Name": {"$regex": KNOWN_EXTERNAL_COURSE_NAME, "$options": "i"}},
        ensure_ascii=False,
    )
    payload = await get_json(
        client,
        "/classes/Courses",
        {
            "where": where,
            "limit": INSPECT_LIMIT,
            "order": "-updatedAt",
            "include": "StatusId,ProductCategory,ProductId,FacilityId,MainLecturerId,MainClassId",
        },
    )
    return payload.get("results", [])


async def inspect_courses(client: httpx.AsyncClient) -> None:
    print("\n=== COURSES MATCHING KNOWN EXTERNAL COURSE ===")
    print(f"Search: {KNOWN_EXTERNAL_COURSE_NAME}")

    rows = await fetch_courses(client)
    print(f"Rows found: {len(rows)}")
    if not rows:
        print("No rows found. Check the exact course name in MyBusiness.")
        return

    candidate_values: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        print(f"\n--- {row.get('Name')} | {row.get('objectId')} ---")
        print(f"createdAt: {row.get('createdAt')}")
        print(f"updatedAt: {row.get('updatedAt')}")
        print(f"StartDate: {row.get('StartDate')}")
        print(f"StatusId: {pretty(row.get('StatusId'))}")
        print(f"ProductCategory: {pretty(row.get('ProductCategory'))}")

        matches = walk(row)
        if not matches:
            print("No external-course-looking fields/values found inside this row.")
        else:
            print("Candidate fields/values:")
            for path, value in matches:
                rendered = pretty(value)
                print(f"{path}: {rendered}")
                candidate_values[path].add(rendered)

        print("Top-level keys:")
        print(", ".join(sorted(row.keys())))

    print("\n=== CANDIDATE FIELD SUMMARY ===")
    if not candidate_values:
        print("No candidate field found in matching rows.")
    else:
        for path, values in sorted(candidate_values.items()):
            print(f"{path}:")
            for value in sorted(values):
                print(f"  - {value}")


async def inspect_yes_no_values(client: httpx.AsyncClient) -> None:
    print("\n=== C_YesNoList VALUES ===")
    try:
        payload = await get_json(
            client,
            "/classes/C_YesNoList",
            {
                "limit": 100,
                "order": "Name",
            },
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic script should continue.
        print(f"C_YesNoList fetch failed: {type(exc).__name__}: {exc}")
        return

    rows = payload.get("results", [])
    for row in rows:
        print(f"{row.get('objectId')}: {row.get('Name') or row.get('Title') or row}")


async def inspect_recent_external_flags(client: httpx.AsyncClient) -> None:
    print("\n=== RECENT COURSES EXTERNAL FLAG SAMPLE ===")
    payload = await get_json(
        client,
        "/classes/Courses",
        {
            "limit": RECENT_COURSES_LIMIT,
            "order": "-updatedAt",
            "include": "ExternalCourse,StatusId,ProductCategory,MainClassId",
            "keys": (
                "objectId,Name,StartDate,updatedAt,ExternalCourse,StatusId,ProductCategory,"
                "MainClassId,AddressToOutSource"
            ),
        },
    )
    rows = payload.get("results", [])
    for row in rows:
        external = row.get("ExternalCourse")
        external_label = None
        external_id = None
        if isinstance(external, dict):
            external_id = external.get("objectId")
            external_label = external.get("Name") or external.get("Title")
        status = row.get("StatusId") if isinstance(row.get("StatusId"), dict) else {}
        category = row.get("ProductCategory") if isinstance(row.get("ProductCategory"), dict) else {}
        class_row = row.get("MainClassId") if isinstance(row.get("MainClassId"), dict) else {}
        print(
            " | ".join(
                [
                    str(row.get("Name")),
                    f"id={row.get('objectId')}",
                    f"external_id={external_id}",
                    f"external_label={external_label}",
                    f"status={status.get('Name')}",
                    f"category={category.get('Name')}",
                    f"class={class_row.get('Name')}",
                    f"address_outsource={row.get('AddressToOutSource')}",
                ]
            )
        )


async def main() -> None:
    settings = get_settings()
    if not settings.mybusiness_app_id or not settings.mybusiness_master_key:
        raise SystemExit("Missing MYBUSINESS_APP_ID or MYBUSINESS_MASTER_KEY in .env")

    headers = {
        "Content-Type": "application/json",
        "X-Parse-Application-Id": settings.mybusiness_app_id,
        "X-Parse-Master-Key": settings.mybusiness_master_key,
    }
    async with httpx.AsyncClient(
        base_url=settings.mybusiness_base_url.rstrip("/"),
        headers=headers,
        timeout=settings.mybusiness_timeout_seconds,
    ) as client:
        await inspect_schema(client)
        await inspect_yes_no_values(client)
        await inspect_courses(client)
        await inspect_recent_external_flags(client)


if __name__ == "__main__":
    asyncio.run(main())
