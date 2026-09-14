from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import Settings
from app.db import Database
from app.services.anthropic_agent_service import AnthropicAgentService
from app.services.knowledge_files import KnowledgeFileService

DEFAULT_QUESTIONS = [
    "שלום",
    "מה תנאי הקבלה לקורס מלגזה?",
    "מתי המועד הקרוב לקורס עבודה בגובה ראשוני?",
    "מה המחיר המעודכן לקורס טכוגרף דיגיטלי לקציני בטיחות?",
    "מה ההבדל בין רענון חומס לרענון אחראי שינוע חומס?",
]


async def measure(
    service: AnthropicAgentService,
    question: str,
) -> tuple[float | None, float, bool, dict | None]:
    session_id, _ = service.create_session()
    started = time.perf_counter()
    first_delta: float | None = None
    final_response: dict | None = None

    async for event in service.ask_stream(session_id, question):
        if event["type"] == "delta" and first_delta is None:
            first_delta = time.perf_counter()
        elif event["type"] == "final":
            final_response = event["response"]

    finished = time.perf_counter()
    ttft = (first_delta - started) if first_delta is not None else None
    success = bool(final_response and not final_response.get("needs_human_review"))
    return ttft, finished - started, success, final_response


async def run(questions: list[str], *, show_answers: bool = False) -> int:
    with TemporaryDirectory(prefix="bambi-anthropic-benchmark-") as temp_dir:
        root = Path(temp_dir)
        settings = Settings(
            LLM_PROVIDER="anthropic",
            DATA_DIR=str(root),
            SQLITE_PATH=str(root / "bambi.db"),
            SESSION_DB_PATH=str(root / "sessions.db"),
        )
        db = Database(settings.sqlite_path)
        db.init_schema()
        service = AnthropicAgentService(settings, db, KnowledgeFileService())

        totals: list[float] = []
        first_tokens: list[float] = []
        failures = 0
        for index, question in enumerate(questions, start=1):
            ttft, total, success, final_response = await measure(service, question)
            totals.append(total)
            if ttft is not None:
                first_tokens.append(ttft)
            if not success:
                failures += 1
            ttft_text = f"{ttft:.2f}s" if ttft is not None else "n/a"
            print(f"{index}. ttft={ttft_text} total={total:.2f}s success={success} question={question}")
            if show_answers and final_response:
                print(f"   answer={final_response.get('answer', '')}")

        print("\nSummary")
        print(f"model={settings.anthropic_model}")
        print(f"questions={len(questions)} failures={failures}")
        if first_tokens:
            print(f"median_ttft={statistics.median(first_tokens):.2f}s")
        print(f"median_total={statistics.median(totals):.2f}s")
        print(f"max_total={max(totals):.2f}s")
        return 1 if failures else 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Measure Claude Sonnet latency through the Bambi agent tools.")
    parser.add_argument("questions", nargs="*", help="Optional Hebrew questions; defaults to five read-only scenarios.")
    parser.add_argument("--show-answers", action="store_true", help="Print final answers for manual quality review.")
    args = parser.parse_args()
    return asyncio.run(run(args.questions or DEFAULT_QUESTIONS, show_answers=args.show_answers))


if __name__ == "__main__":
    raise SystemExit(main())
