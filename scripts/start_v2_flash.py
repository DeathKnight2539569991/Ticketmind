"""Launch TicketMind with the frozen synthetic-v2 corpus and Flash decision model.

This is a runtime-only profile: do not edit .env or change the caller's environment.
All other settings (DB, credentials, Judge, ports, etc.) come from the existing .env.
"""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

PROFILE = {
    "TICKETMIND_KNOWLEDGE_DATASET": "synthetic-v2-e5b5a59a7e1481ad3b095d518772354155d891e51ad2a367cf5f7be26540228f",
    "TICKETMIND_DECISION_MODEL": "qwen3.8-flash",
    "TICKETMIND_RETRIEVAL_MODE": "bm25",
}


def profile_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Overlay this launch's three selections; preserve secrets and all other settings."""
    environment = dict(os.environ if base is None else base)
    environment.update(PROFILE)
    return environment


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    print("TicketMind profile: synthetic-v2 / qwen3.8-flash / bm25", flush=True)
    return subprocess.call(
        [sys.executable, str(ROOT / "scripts" / "start_local.py"), *args],
        cwd=ROOT,
        env=profile_environment(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
