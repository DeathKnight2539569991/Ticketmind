"""Zero-provider verification; real databases require explicit flags.

Tests use model/vector doubles. --db uses UUID PostgreSQL schemas; --milvus also
uses guarded UUID collections and a read-only existing frozen synthetic index.
"""
import argparse
import contextlib
from datetime import UTC, datetime
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from uuid import uuid4
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", action="store_true", help="Enable isolated real PostgreSQL tests")
    parser.add_argument("--milvus", action="store_true", help="Requires --db and existing local Milvus/index")
    args = parser.parse_args()
    if args.milvus and not args.db:
        parser.error("--milvus requires --db")
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    directory = ROOT / "data/cache/verification" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex)
    directory.mkdir(parents=True)
    tempfile.tempdir = str(directory)
    os.environ.update(TEMP=str(directory), TMP=str(directory), TICKETMIND_RUN_DB_TESTS=str(int(args.db)),
                      TICKETMIND_RUN_MILVUS_TESTS=str(int(args.milvus)), PYTHONDONTWRITEBYTECODE="1")
    database_url = None
    if args.db:
        from ticketmind.core.config import Settings
        database_url = os.getenv("TICKETMIND_TEST_DATABASE_URL") or Settings().database_url.unicode_string()
        os.environ["TICKETMIND_TEST_DATABASE_URL"] = database_url
    import pytest
    captured = io.StringIO()
    print(f"Verification evidence: {directory}", flush=True)
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        code = pytest.main(["tests", "-q", "--tb=short", "-p", "no:cacheprovider",
                           "--basetemp", str(directory / "tmp"), "--junitxml", str(directory / "results.xml")])
    output = captured.getvalue()
    if database_url:
        output = output.replace(database_url, "[REDACTED_DATABASE_URL]")
    output = re.sub(r"(?i)(postgresql(?:\+\w+)?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", output)
    (directory / "results.log").write_text(output, encoding="utf-8")
    suites = ET.parse(directory / "results.xml").getroot().findall("testsuite")
    counts = {key: sum(int(s.get(key, 0)) for s in suites) for key in ("tests", "failures", "errors", "skipped")}
    summary = {"completed_at": datetime.now(UTC).isoformat(), "exit_code": int(code), **counts,
        "passed": counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"],
        "duration_seconds": sum(float(s.get("time", 0)) for s in suites),
        "real_postgresql": args.db, "real_milvus": args.milvus, "provider_calls": 0,
        "model_and_vectors": "deterministic test doubles", "evidence_directory": directory.relative_to(ROOT).as_posix()}
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output[-12000:])
    print(json.dumps(summary, ensure_ascii=False))
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
