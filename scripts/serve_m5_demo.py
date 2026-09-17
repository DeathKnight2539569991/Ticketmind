"""Temporary real PostgreSQL schema + HTTP API, no model or Milvus calls."""
import argparse
import json
import secrets
import signal
from pathlib import Path

import uvicorn

from ticketmind.core.config import AuthSettings
from ticketmind.db.testing import isolated_database
from ticketmind.main import create_app
from ticketmind.workbench.demo import DemoRunner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    directory = Path("data/cache/m5")
    directory.mkdir(parents=True, exist_ok=True)
    # Fresh credentials only belong to this isolated demo; never load production tokens.
    auth = AuthSettings(_env_file=None, operator_token=secrets.token_urlsafe(32), reviewer_token=secrets.token_urlsafe(32))
    with isolated_database() as (_, factory, schema):
        credentials = directory / f"demo-{schema}.json"
        credentials.write_text(json.dumps({"api_url": f"http://127.0.0.1:{args.port}", "schema": schema,
            "operator_token": auth.operator_token.get_secret_value(), "reviewer_token": auth.reviewer_token.get_secret_value()}, indent=2), encoding="utf-8")
        print(f"SYNTHETIC DEMO ONLY. Local credentials: {credentials.resolve()}", flush=True)
        try:
            server = uvicorn.Server(uvicorn.Config(create_app(session_factory=factory, runner=DemoRunner(), auth_settings=auth),
                        host="127.0.0.1", port=args.port, log_level="warning"))
            if hasattr(signal, "SIGBREAK"):
                signal.signal(signal.SIGBREAK, lambda *_: setattr(server, "should_exit", True))
            server.run()
        finally:
            credentials.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
