import os
import pytest

from ticketmind.documents.index import docs_collection, docs_manifest
from ticketmind.documents.markdown import CHUNK_MAX_BYTES, parse_markdown
from ticketmind.retrieval.schemas import RetrievalError


def test_markdown_stable_sections_and_fences():
    source = "# Example\n\n## First\n\nUseful body.\n\n~~~md\n# not a heading\n~~~\n\n## Second\n\nAnother body.\n"
    first = parse_markdown("example", source)
    assert first == parse_markdown("example", source.replace("\n", "\r\n"))
    assert [chunk.section for chunk in first.chunks] == ["First", "Second"]
    assert "# not a heading" in first.chunks[0].text
    assert first.chunks[0].chunk_id == "example:001:001"
    assert first.chunks[0].content_hash != parse_markdown("example", source.replace("Example", "Changed")).chunks[0].content_hash


def test_markdown_long_paragraph_chunks_under_limit():
    document = parse_markdown("large", "# Large\n\n## Section\n\n" + "中" * 7000)
    assert len(document.chunks) > 1
    assert all(len(chunk.text.encode()) <= CHUNK_MAX_BYTES for chunk in document.chunks)


def test_docs_namespace_is_independent():
    assert docs_collection("synthetic-product-docs-v1").startswith("docs_bm25_")
    assert docs_manifest("synthetic-product-docs-v1")["kind"] == "product_docs_bm25"


class FakeDocsIndex:
    def __init__(self):
        self.rows = {}

    def ensure(self, dataset):
        assert dataset.collection_name.startswith("docs_bm25_")
        return dataset.collection_name

    def matches(self, dataset, chunk):
        row = self.rows.get(chunk.source_id)
        return row is not None and row["entity"]["content_hash"] == chunk.content_hash

    def upsert(self, dataset, document, chunk):
        self.rows[chunk.source_id] = {"entity": {"source_id": chunk.source_id,
            "docs_version": dataset.version, "content_hash": chunk.content_hash},
            "distance": 1.0, "search_text": (document.title + " " + chunk.section + " " + chunk.text).lower()}

    def search(self, dataset, query, *, limit, offset=0):
        matches = [row for row in self.rows.values() if query.lower() in row["search_text"]]
        matches.sort(key=lambda row: row["entity"]["source_id"])
        return matches[offset:offset + limit]


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("TICKETMIND_RUN_DB_TESTS") != "1", reason="requires explicit PostgreSQL test opt-in")
def test_import_sync_retrieve_and_changed_document_in_isolated_postgres(tmp_path, monkeypatch):
    from ticketmind.db.testing import isolated_database
    from ticketmind.documents.importer import import_markdown, sync_docs
    from ticketmind.documents.models import DocumentChunk
    from ticketmind.documents.service import DocStore, retrieve_docs
    from ticketmind.documents import service

    path = tmp_path / "product.md"
    path.write_text("# Product\n\n## Export\n\nexporttoken: use preview.\n\n## Import\n\nimporttoken: inspect delimiter.\n", encoding="utf-8")
    index = FakeDocsIndex()
    monkeypatch.setattr(service, "MilvusDocsIndex", lambda _client, _timeout: index)
    with isolated_database() as (_engine, factory, _schema):
        store = DocStore(factory)
        assert store.metadata()["status"] == "not_ready"
        assert store.metadata()["catalog_hash"] is None
        assert import_markdown(factory, path)["changed"] == 1
        assert import_markdown(factory, path)["changed"] == 0
        assert sync_docs(factory, index)["indexed"] == 2
        assert sync_docs(factory, index)["indexed"] == 0
        assert store.metadata()["status"] == "ready"
        before = store.metadata()["catalog_hash"]
        hits = retrieve_docs("importtoken", client=object(), store=store)
        assert len(hits) == 1
        assert hits[0].source_id.startswith("docs:")
        assert hits[0].doc_id == "product" and hits[0].section == "Import"
        assert "exporttoken" not in hits[0].text
        assert hits[0].synthetic and hits[0].docs_version == store.version
        index.rows["SYN-HIST-V2-001"] = {"entity": {"source_id": "SYN-HIST-V2-001",
            "docs_version": store.version, "content_hash": "casehash"}, "distance": 2.0,
            "search_text": "importtoken case"}
        assert [hit.source_id for hit in retrieve_docs("importtoken", client=object(), store=store)] == [hits[0].source_id]
        with pytest.raises(RetrievalError, match="docs_dense_index_not_ready"):
            retrieve_docs("importtoken", client=object(), store=store, mode="dense")
        record = {}
        hybrid_hits = retrieve_docs("importtoken", client=object(), store=store, mode="hybrid", record=record)
        assert hybrid_hits and hybrid_hits[0].mode == "bm25"
        assert record["channels"]["dense"]["status"] == "skipped"

        path.write_text("# Product Revised\n\n## Export\n\nexporttoken: use preview.\n\n## Import\n\nimporttoken: inspect delimiter.\n", encoding="utf-8")
        assert import_markdown(factory, path)["changed"] == 1
        assert store.metadata()["catalog_hash"] != before
        assert store.metadata()["status"] == "not_ready"
        assert retrieve_docs("importtoken", client=object(), store=store) == []
        assert sync_docs(factory, index)["indexed"] == 2
        changed = retrieve_docs("importtoken", client=object(), store=store)
        assert changed[0].title == "Product Revised"
        assert changed[0].content_hash != hits[0].content_hash
        with factory() as session:
            chunks = session.query(DocumentChunk).all()
            assert len(chunks) == 2
