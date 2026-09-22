"""Milvus contains derived search text/vectors and stable identity, never authoritative bodies."""
import hashlib
import json
import math
import string

from pymilvus import DataType, Function, FunctionType

from ticketmind.knowledge.models import PRODUCTION_DATASET
from ticketmind.retrieval.case_collection import EMBEDDING_DIMENSION, TEXT_MAX_BYTES
from ticketmind.retrieval.schemas import RetrievalError


def production_manifest():
    return {"schema_version": 2, "corpus_version": PRODUCTION_DATASET,
            "embedding_model": "text-embedding-v4", "dimension": EMBEDDING_DIMENSION,
            "analyzer": {"tokenizer": {"type": "jieba", "dict": ["_default_"], "mode": "search"},
                         "filter": ["lowercase", {"type": "stop", "stop_words": list(string.whitespace + string.punctuation + "，。；：！？（）【】、“”‘’")}]},
            "bm25_k1": 1.2, "bm25_b": 0.75}


def manifest_text(manifest):
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def collection_description(manifest):
    """Keep legacy short descriptions; fingerprint larger manifests under Milvus' 1024-byte limit.

    PostgreSQL retains the full immutable manifest and collection name.  The
    digest is only a compact integrity marker for Milvus' description field;
    the full analyzer/schema still undergoes validation below.
    """
    full = manifest_text(manifest)
    encoded = full.encode("utf-8")
    if len(encoded) <= 1024:
        return full
    return "manifest-sha256:" + hashlib.sha256(encoded).hexdigest()


def production_collection():
    return "knowledge_" + hashlib.sha256(manifest_text(production_manifest()).encode()).hexdigest()[:24]


def validate_vector(vector):
    if len(vector) != EMBEDDING_DIMENSION or not all(math.isfinite(x) for x in vector) or not any(vector):
        raise RetrievalError("invalid_embedding_vector")


class MilvusKnowledgeIndex:
    def __init__(self, client, timeout=10):
        self.client = client
        self.budget = timeout if callable(timeout) else lambda: timeout

    @staticmethod
    def text_identity(dataset):
        manifest = {"schema_version": 1, "kind": "bm25_only", "corpus_version": dataset.version,
                    "analyzer": dataset.manifest["analyzer"], "bm25_k1": 1.2, "bm25_b": 0.75}
        return "knowledge_bm25_" + hashlib.sha256(manifest_text(manifest).encode()).hexdigest()[:24], manifest

    def text_exists(self, dataset):
        name, _ = self.text_identity(dataset)
        return self.client.has_collection(collection_name=name, timeout=self.budget())

    def validate_text(self, dataset, name):
        expected, manifest = self.text_identity(dataset)
        if name != expected or not self.client.has_collection(collection_name=name, timeout=self.budget()):
            raise RetrievalError("bm25_collection_missing")
        description = self.client.describe_collection(collection_name=name, timeout=self.budget())
        fields = {field["name"]: field for field in description["fields"]}
        if (description.get("description") != collection_description(manifest)
                or not {"source_id", "corpus_version", "content_hash", "bm25_text", "sparse"} <= fields.keys()):
            raise RetrievalError("bm25_collection_schema_mismatch")
        analyzer = fields["bm25_text"].get("params", {}).get("analyzer_params")
        if isinstance(analyzer, str):
            analyzer = json.loads(analyzer)
        if analyzer != manifest["analyzer"] or not any(
                fn.get("type") == FunctionType.BM25 and fn.get("input_field_names") == ["bm25_text"]
                and fn.get("output_field_names") == ["sparse"] for fn in description.get("functions", [])):
            raise RetrievalError("bm25_collection_analyzer_mismatch")
        actual = self.client.describe_index(collection_name=name, index_name="sparse_bm25", timeout=self.budget())
        if actual.get("field_name") != "sparse" or actual.get("metric_type") != "BM25":
            raise RetrievalError("bm25_collection_index_mismatch")

    def ensure_text(self, dataset):
        name, manifest = self.text_identity(dataset)
        if not self.client.has_collection(collection_name=name, timeout=self.budget()):
            schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False,
                                               description=collection_description(manifest))
            schema.add_field("source_id", DataType.VARCHAR, is_primary=True, max_length=128)
            schema.add_field("corpus_version", DataType.VARCHAR, max_length=128)
            schema.add_field("content_hash", DataType.VARCHAR, max_length=64)
            schema.add_field("bm25_text", DataType.VARCHAR, max_length=TEXT_MAX_BYTES,
                             enable_analyzer=True, analyzer_params=manifest["analyzer"])
            schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
            schema.add_function(Function(name="case_bm25", input_field_names=["bm25_text"],
                output_field_names=["sparse"], function_type=FunctionType.BM25))
            indexes = self.client.prepare_index_params()
            indexes.add_index(field_name="sparse", index_name="sparse_bm25", index_type="SPARSE_INVERTED_INDEX",
                metric_type="BM25", params={"inverted_index_algo": "DAAT_MAXSCORE", "bm25_k1": 1.2, "bm25_b": 0.75})
            self.client.create_collection(collection_name=name, schema=schema, index_params=indexes,
                consistency_level="Strong", timeout=self.budget())
        self.validate_text(dataset, name)
        return name

    def text_matches(self, dataset, case, name):
        rows = self.client.get(collection_name=name, ids=[case.source_id],
            output_fields=["source_id", "corpus_version", "content_hash"],
            consistency_level="Strong", timeout=self.budget())
        return (len(rows) == 1 and rows[0].get("corpus_version") == dataset.version
                and rows[0].get("content_hash") == case.content_hash)

    def upsert_text(self, dataset, case, name):
        if len(case.content.encode("utf-8")) > TEXT_MAX_BYTES:
            raise RetrievalError("knowledge_index_text_too_long")
        self.client.upsert(collection_name=name, data=[{"source_id": case.source_id,
            "corpus_version": dataset.version, "content_hash": case.content_hash,
            "bm25_text": case.content.lower()}], timeout=self.budget())
        if not self.text_matches(dataset, case, name):
            raise RetrievalError("bm25_index_readback_failed")

    def validate(self, dataset):
        client, name = self.client, dataset.collection_name
        if not client.has_collection(collection_name=name, timeout=self.budget()):
            raise RetrievalError("retrieval_collection_missing")
        description = client.describe_collection(collection_name=name, timeout=self.budget())
        if description.get("description") != collection_description(dataset.manifest):
            raise RetrievalError("collection_version_mismatch")
        fields = {field["name"]: field for field in description["fields"]}
        required = {"source_id", "corpus_version", "bm25_text", "embedding", "sparse"}
        required.add("content_hash" if dataset.manifest["schema_version"] == 2 else "text")
        if not required <= fields.keys() or int(fields["embedding"]["params"]["dim"]) != EMBEDDING_DIMENSION:
            raise RetrievalError("collection_schema_mismatch")
        analyzer = fields["bm25_text"].get("params", {}).get("analyzer_params")
        if isinstance(analyzer, str):
            analyzer = json.loads(analyzer)
        if analyzer != dataset.manifest["analyzer"] or not any(
                fn.get("type") == FunctionType.BM25 and fn.get("input_field_names") == ["bm25_text"]
                and fn.get("output_field_names") == ["sparse"] for fn in description.get("functions", [])):
            raise RetrievalError("collection_analyzer_mismatch")
        for field, index, metric in (("embedding", "embedding_flat", "COSINE"), ("sparse", "sparse_bm25", "BM25")):
            actual = client.describe_index(collection_name=name, index_name=index, timeout=self.budget())
            if actual.get("field_name") != field or actual.get("metric_type") != metric:
                raise RetrievalError("collection_index_mismatch")

    def ensure(self, dataset):
        client, name = self.client, dataset.collection_name
        if client.has_collection(collection_name=name, timeout=self.budget()):
            return self.validate(dataset)
        schema = client.create_schema(auto_id=False, enable_dynamic_field=False, description=collection_description(dataset.manifest))
        schema.add_field("source_id", DataType.VARCHAR, is_primary=True, max_length=128)
        schema.add_field("corpus_version", DataType.VARCHAR, max_length=128)
        if dataset.manifest["schema_version"] == 2:
            schema.add_field("content_hash", DataType.VARCHAR, max_length=64)
        else:
            schema.add_field("text", DataType.VARCHAR, max_length=TEXT_MAX_BYTES)
        schema.add_field("bm25_text", DataType.VARCHAR, max_length=TEXT_MAX_BYTES, enable_analyzer=True,
                         analyzer_params=dataset.manifest["analyzer"])
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=EMBEDDING_DIMENSION)
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(Function(name="case_bm25", input_field_names=["bm25_text"], output_field_names=["sparse"], function_type=FunctionType.BM25))
        indexes = client.prepare_index_params()
        indexes.add_index(field_name="embedding", index_name="embedding_flat", index_type="FLAT", metric_type="COSINE")
        indexes.add_index(field_name="sparse", index_name="sparse_bm25", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25",
                          params={"inverted_index_algo": "DAAT_MAXSCORE", "bm25_k1": 1.2, "bm25_b": 0.75})
        client.create_collection(collection_name=name, schema=schema, index_params=indexes, consistency_level="Strong", timeout=self.budget())
        self.validate(dataset)

    def matches(self, dataset, case):
        modern = dataset.manifest["schema_version"] == 2
        fields = ["source_id", "corpus_version", "content_hash" if modern else "text"]
        rows = self.client.get(collection_name=dataset.collection_name, ids=[case.source_id], output_fields=fields,
                               consistency_level="Strong", timeout=self.budget())
        return len(rows) == 1 and rows[0]["corpus_version"] == dataset.version and (
            rows[0].get("content_hash") == case.content_hash if modern else rows[0].get("text") == case.content)

    def upsert(self, dataset, case, vector):
        validate_vector(vector)
        if len(case.content.encode()) > TEXT_MAX_BYTES:
            raise RetrievalError("knowledge_index_text_too_long")
        data = {"source_id": case.source_id, "corpus_version": dataset.version,
                "bm25_text": case.content.lower(), "embedding": vector}
        data.update({"content_hash": case.content_hash} if dataset.manifest["schema_version"] == 2 else {"text": case.content})
        self.client.upsert(collection_name=dataset.collection_name, data=[data], timeout=self.budget())
        if not self.matches(dataset, case):
            raise RetrievalError("index_readback_failed")

    def iter_source_id_batches(self, dataset, *, batch_size=1000, collection_name=None):
        if not 1 <= batch_size <= 1000:
            raise ValueError("batch_size must be 1..1000")
        name = collection_name or dataset.collection_name
        if not self.client.has_collection(collection_name=name, timeout=self.budget()):
            return
        last_source_id = None
        while True:
            filter_expr = "" if last_source_id is None else f"source_id > {json.dumps(last_source_id)}"
            rows = self.client.query(collection_name=name, filter=filter_expr,
                output_fields=["source_id"], limit=batch_size, order_by=["source_id:asc"],
                consistency_level="Strong", timeout=self.budget())
            source_ids = [row["source_id"] for row in rows]
            if not source_ids:
                break
            if source_ids != sorted(set(source_ids)) or (
                    last_source_id is not None and source_ids[0] <= last_source_id):
                raise RetrievalError("index_scan_order_invalid")
            yield source_ids
            if len(source_ids) < batch_size:
                break
            last_source_id = source_ids[-1]

    def delete_source_ids(self, dataset, source_ids, *, collection_name=None):
        source_ids = list(dict.fromkeys(source_ids))
        if not source_ids:
            return
        name = collection_name or dataset.collection_name
        if self.client.has_collection(collection_name=name, timeout=self.budget()):
            self.client.delete(collection_name=name, ids=source_ids, timeout=self.budget())
            if self.client.get(collection_name=name, ids=source_ids, output_fields=["source_id"],
                               consistency_level="Strong", timeout=self.budget()):
                raise RetrievalError("index_delete_readback_failed")

    def delete(self, dataset, case):
        self.delete_source_ids(dataset, [case.source_id])
        if dataset.bm25_collection_name:
            self.delete_source_ids(dataset, [case.source_id], collection_name=dataset.bm25_collection_name)
