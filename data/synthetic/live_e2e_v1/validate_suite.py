"""Validate the isolated synthetic live-E2E fixture without DB, Milvus or LLM calls."""
import json
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
