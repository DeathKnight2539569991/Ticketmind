"""Explicit Markdown import command; never invokes a model or embedding API."""
import argparse
import json
from pathlib import Path

from ticketmind.core.config import MilvusSettings, ProcessingSettings
from ticketmind.db.session import SesstionLocal
from ticketmind.documents.importer import import_markdown, sync_docs
from ticketmind.documents.index import MilvusDocsIndex
from ticketmind.retrieval.milvus_client import build_milvus_client


def main():
    parser = argparse.ArgumentParser(description="Import synthetic Markdown product docs")
    parser.add_argument("path", type=Path, help="Markdown file or directory")
    parser.add_argument("--version", default=ProcessingSettings().docs_dataset)
    parser.add_argument("--no-index", action="store_true", help="stage PostgreSQL content without Milvus sync")
    args = parser.parse_args()
    result = import_markdown(SesstionLocal, args.path, version=args.version)
    if not args.no_index:
        settings = MilvusSettings()
        client = build_milvus_client(settings)
        try:
            result["index"] = sync_docs(SesstionLocal, MilvusDocsIndex(client, settings.timeout_seconds), version=args.version)
        finally:
            client.close()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
