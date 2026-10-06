"""Milvus BM25 index for product-doc chunks; all bodies come from PostgreSQL."""
import hashlib
import json
import string

from pymilvus import DataType, Function, FunctionType

from ticketmind.retrieval.case_collection import TEXT_MAX_BYTES
from ticketmind.retrieval.schemas import RetrievalError


def docs_manifest(version: str) -> dict:
    return {"schema_version": 1, "kind": "product_docs_bm25", "docs_version": version,
            "analyzer": {"tokenizer": {"type": "jieba", "dict": ["_default_"], "mode": "search"},
                         "filter": ["lowercase", {"type": "stop", "stop_words": list(string.whitespace + string.punctuation + "，。；：！？（）【】、“”‘’")}]},
            "bm25_k1": 1.2, "bm25_b": 0.75}


def docs_collection(version: str) -> str:
    manifest = _manifest_text(docs_manifest(version))
    return "docs_bm25_" + hashlib.sha256(manifest.encode()).hexdigest()[:24]


def _manifest_text(manifest: dict) -> str:
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _description(manifest: dict) -> str:
    encoded = _manifest_text(manifest).encode("utf-8")
    return encoded.decode("utf-8") if len(encoded) <= 1024 else "manifest-sha256:" + hashlib.sha256(encoded).hexdigest()


class MilvusDocsIndex:
    def __init__(self, client, timeout=10):
        self.client = client
        self.budget = timeout if callable(timeout) else lambda: timeout

    def validate(self, dataset):
        name = dataset.collection_name
        if name != docs_collection(dataset.version) or dataset.manifest != docs_manifest(dataset.version):
            raise RetrievalError("docs_collection_version_mismatch")
        if not self.client.has_collection(collection_name=name, timeout=self.budget()):
            raise RetrievalError("docs_collection_missing")
        description = self.client.describe_collection(collection_name=name, timeout=self.budget())
        fields = {field["name"]: field for field in description["fields"]}
        if description.get("description") != _description(dataset.manifest) or not {
                "source_id", "docs_version", "content_hash", "bm25_text", "sparse"} <= fields.keys():
            raise RetrievalError("docs_collection_schema_mismatch")
        analyzer = fields["bm25_text"].get("params", {}).get("analyzer_params")
        if isinstance(analyzer, str):
            analyzer = json.loads(analyzer)
        if analyzer != dataset.manifest["analyzer"] or not any(
                function.get("type") == FunctionType.BM25 and function.get("input_field_names") == ["bm25_text"]
                and function.get("output_field_names") == ["sparse"] for function in description.get("functions", [])):
            raise RetrievalError("docs_collection_analyzer_mismatch")
        actual = self.client.describe_index(collection_name=name, index_name="sparse_bm25", timeout=self.budget())
        if actual.get("field_name") != "sparse" or actual.get("metric_type") != "BM25":
            raise RetrievalError("docs_collection_index_mismatch")
        return name

    def ensure(self, dataset):
        name = dataset.collection_name
        if not self.client.has_collection(collection_name=name, timeout=self.budget()):
            schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False,
                                               description=_description(dataset.manifest))
            schema.add_field("source_id", DataType.VARCHAR, is_primary=True, max_length=128)
            schema.add_field("docs_version", DataType.VARCHAR, max_length=128)
            schema.add_field("content_hash", DataType.VARCHAR, max_length=64)
            schema.add_field("bm25_text", DataType.VARCHAR, max_length=TEXT_MAX_BYTES,
                             enable_analyzer=True, analyzer_params=dataset.manifest["analyzer"])
            schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
            schema.add_function(Function(name="docs_bm25", input_field_names=["bm25_text"],
                output_field_names=["sparse"], function_type=FunctionType.BM25))
            indexes = self.client.prepare_index_params()
            indexes.add_index(field_name="sparse", index_name="sparse_bm25", index_type="SPARSE_INVERTED_INDEX",
                metric_type="BM25", params={"inverted_index_algo": "DAAT_MAXSCORE", "bm25_k1": 1.2, "bm25_b": 0.75})
            self.client.create_collection(collection_name=name, schema=schema, index_params=indexes,
                                          consistency_level="Strong", timeout=self.budget())
        return self.validate(dataset)

    def matches(self, dataset, chunk):
        rows = self.client.get(collection_name=dataset.collection_name, ids=[chunk.source_id],
            output_fields=["source_id", "docs_version", "content_hash"],
            consistency_level="Strong", timeout=self.budget())
        return len(rows) == 1 and rows[0].get("docs_version") == dataset.version and rows[0].get("content_hash") == chunk.content_hash

    def upsert(self, dataset, document, chunk):
        search_text = f"{document.title}\n{chunk.section}\n{chunk.text}".lower()
        if len(search_text.encode("utf-8")) > TEXT_MAX_BYTES:
            raise RetrievalError("docs_index_text_too_long")
        self.client.upsert(collection_name=dataset.collection_name, data=[{
            "source_id": chunk.source_id, "docs_version": dataset.version,
            "content_hash": chunk.content_hash, "bm25_text": search_text}], timeout=self.budget())
        if not self.matches(dataset, chunk):
            raise RetrievalError("docs_index_readback_failed")

    def search(self, dataset, query, *, limit, offset=0):
        args = dict(collection_name=self.validate(dataset), data=[query.lower()], anns_field="sparse",
                    search_params={"metric_type": "BM25"}, output_fields=["source_id", "docs_version", "content_hash"],
                    limit=limit, timeout=self.budget(), consistency_level="Strong")
        if offset:
            args["offset"] = offset
        rows = self.client.search(**args)
        if len(rows) != 1:
            raise RetrievalError("docs_invalid_response")
        return rows[0]
