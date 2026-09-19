"""Validate the isolated synthetic live-E2E fixture without DB, Milvus or LLM calls."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ticketmind.api.schemas.tickets import TicketCreate
from ticketmind.knowledge.corpus import build_case_text, load_historical_cases
from ticketmind.retrieval.case_collection import SOURCE_ID_MAX_BYTES, TEXT_MAX_BYTES

ROOT = Path(__file__).resolve().parent


def rows(name: str) -> list[dict]:
    data = [json.loads(line) for line in (ROOT / name).read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if not data:
        raise ValueError(f"{name} is empty")
    return data


def main() -> None:
    history = load_historical_cases(ROOT / "historical_cases.jsonl")
    source_ids = {case.source_id for case in history}
    case_one = next((case for case in history if case.source_id == "SYN-LIVE-E2E-2026-09-001"), None)
    if case_one is None:
        raise ValueError("missing corrected case 001")
    utc = datetime.fromisoformat("2026-09-09T16:30:00+00:00")
    if utc.astimezone(timezone(timedelta(hours=8))).isoformat() != "2026-09-10T00:30:00+08:00":
        raise ValueError("incorrect UTC to Shanghai boundary conversion")
    text_one = build_case_text(case_one)
    if ("2026-09-09T16:30:00Z" not in text_one or
            "2026-09-10T00:00:00Z" not in text_one or
            "2026-09-09T16:00:00Z" not in text_one or "23:00" in text_one):
        raise ValueError("corrected case 001 date-boundary evidence is missing or stale")
    old_path = ROOT.parent / "live_e2e_v1" / "historical_cases.jsonl"
    if old_path.exists():
        old_history = load_historical_cases(old_path)
        if [case.model_dump() for case in history[1:]] != [case.model_dump() for case in old_history[1:]]:
            raise ValueError("historical cases 002-010 unexpectedly changed")

    for case in history:
        if len(case.source_id.encode("utf-8")) > SOURCE_ID_MAX_BYTES:
            raise ValueError(f"source_id too long: {case.source_id}")
        if len(build_case_text(case).encode("utf-8")) > TEXT_MAX_BYTES:
            raise ValueError(f"source text too long: {case.source_id}")
    tests = rows("test_tickets.jsonl")
    checks = rows("review_checks.jsonl")
    followups = rows("customer_followups.jsonl")
    test_ids = [entry["case_id"] for entry in tests]
    if len(test_ids) != len(set(test_ids)) or len(test_ids) != len(checks):
        raise ValueError("duplicate or unpaired test IDs")
    if set(test_ids) != {entry["case_id"] for entry in checks}:
        raise ValueError("test and checks IDs differ")
    if any(entry.get("synthetic") is not True for entry in tests):
        raise ValueError("tests must be explicitly synthetic")
    for entry in tests:
        TicketCreate.model_validate(entry["input"])
    test_one = next((t for t in tests if t["case_id"] == "SYN-LIVE-TEST-01"), None)
    if not test_one or "2026-09-09T16:30:00Z" not in test_one["input"]["body"]:
        raise ValueError("TEST-01 does not match corrected date-boundary case")
    test_17 = next((t for t in tests if t["case_id"] == "SYN-LIVE-TEST-17"), None)
    if not test_17 or "我还没有提供" in test_17["input"]["body"]:
        raise ValueError("TEST-17 contains explicit missing-fact hints")

    for check in checks:
        if check.get("label_status") != "draft_not_independently_reviewed":
            raise ValueError("review checks have not been independently reviewed")
        if set(check["reference_source_ids"]) - source_ids:
            raise ValueError(f"unknown source in {check['case_id']}")
    for followup in followups:
        if followup["case_id"] not in test_ids or followup["message_kind"] != "customer_update":
            raise ValueError("invalid follow-up")
        if not followup["body"].strip():
            raise ValueError("empty follow-up")
    print(f"VALIDATED: {len(history)} synthetic historical cases, {len(tests)} tickets, "
          f"{len(checks)} isolated draft review checks, {len(followups)} follow-ups")
    print("This checks fixture structure only; no real DB, Milvus, model or UI run was executed.")


if __name__ == "__main__":
    main()
