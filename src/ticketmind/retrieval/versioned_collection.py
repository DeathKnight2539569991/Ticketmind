"""Immutable collection identity; the legacy Dense collection is never altered."""
import hashlib
import json
import re
import string

from pymilvus import DataType, Function, FunctionType

from ticketmind.knowledge.corpus import build_case_text
from ticketmind.retrieval.case_collection import EMBEDDING_DIMENSION, TEXT_MAX_BYTES, CASE_COLLECTION, LEGACY_CORPUS_VERSION
from ticketmind.retrieval.schemas import RetrievalError


def analyzer_for(corpus):
    # Vocabulary comes only from indexed documents, never evaluation labels/queries.
    terms = sorted(set(re.findall(r"[a-z0-9]+(?:[_.-][a-z0-9]+)*",
        "\n".join(build_case_text(case) for case in corpus.cases.values()).lower())))
    return {"tokenizer": {"type": "jieba", "dict": ["_default_", *terms], "mode": "search"},
            "filter": ["lowercase", {"type": "stop", "stop_words": list(string.whitespace + string.punctuation + "，。；：！？（）【】、“”‘’")}]}


def manifest_for(corpus, model="text-embedding-v4"):
    if model != "text-embedding-v4":
        raise RetrievalError("embedding_model_mismatch")
    return {"schema_version": 1, "corpus_version": corpus.version, "embedding_model": model,
            "dimension": EMBEDDING_DIMENSION, "analyzer": analyzer_for(corpus),
            "bm25_k1": 1.2, "bm25_b": 0.75}


def manifest_text(corpus, model="text-embedding-v4"):
    return json.dumps(manifest_for(corpus, model), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def collection_for(corpus, model="text-embedding-v4"):
    digest = hashlib.sha256(manifest_text(corpus, model).encode()).hexdigest()[:24]
    return "historical_cases_m3_" + digest


def selected_collection(corpus, mode, model="text-embedding-v4"):
    # Only this immutable corpus may use the unversioned legacy Dense baseline.
    if mode == "dense" and corpus.version == LEGACY_CORPUS_VERSION:
        return CASE_COLLECTION
    return collection_for(corpus, model)


def validate_collection(client, corpus, *, model="text-embedding-v4", timeout, require_data=False):
    budget = timeout if callable(timeout) else lambda: timeout
    name = collection_for(corpus, model)
    if not client.has_collection(collection_name=name, timeout=budget()):
        raise RetrievalError("retrieval_collection_missing")
    description = client.describe_collection(collection_name=name, timeout=budget())
    if description.get("description") != manifest_text(corpus, model):
        raise RetrievalError("collection_version_mismatch")
    fields = {field["name"]: field for field in description["fields"]}
    if (int(fields.get("embedding", {}).get("params", {}).get("dim", 0)) != EMBEDDING_DIMENSION
            or not {"source_id", "text", "bm25_text", "sparse", "corpus_version"} <= fields.keys()):
        raise RetrievalError("collection_schema_mismatch")
    analyzer = fields["bm25_text"].get("params", {}).get("analyzer_params")
    if isinstance(analyzer, str):
        analyzer = json.loads(analyzer)
    if analyzer != analyzer_for(corpus) or not any(
            fn.get("type") == FunctionType.BM25 and fn.get("input_field_names") == ["bm25_text"]
            and fn.get("output_field_names") == ["sparse"] for fn in description.get("functions", [])):
        raise RetrievalError("collection_analyzer_mismatch")
    for index_name, metric, field in (("embedding_flat", "COSINE", "embedding"), ("sparse_bm25", "BM25", "sparse")):
        index = client.describe_index(collection_name=name, index_name=index_name, timeout=budget())
        if index.get("metric_type") != metric or index.get("field_name") != field:
            raise RetrievalError("collection_index_mismatch")
    if require_data:
        counts = client.query(collection_name=name, filter="", output_fields=["count(*)"],
                              consistency_level="Strong", timeout=budget())
        if not counts or counts[0]["count(*)"] != len(corpus.cases):
            raise RetrievalError("collection_data_incomplete")
    return name


def create_versioned_collection(client, corpus, *, model="text-embedding-v4", timeout):
    name = collection_for(corpus, model)
    if client.has_collection(collection_name=name, timeout=timeout):
        return validate_collection(client, corpus, model=model, timeout=timeout)
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False,
                                  description=manifest_text(corpus, model))
    schema.add_field("source_id", DataType.VARCHAR, is_primary=True, max_length=128)
    schema.add_field("text", DataType.VARCHAR, max_length=TEXT_MAX_BYTES)
    schema.add_field("corpus_version", DataType.VARCHAR, max_length=128)
    schema.add_field("bm25_text", DataType.VARCHAR, max_length=TEXT_MAX_BYTES,
                     enable_analyzer=True, analyzer_params=analyzer_for(corpus))
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=EMBEDDING_DIMENSION)
    schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
    schema.add_function(Function(name="case_bm25", input_field_names=["bm25_text"],
        output_field_names=["sparse"], function_type=FunctionType.BM25))
    indexes = client.prepare_index_params()
    indexes.add_index(field_name="embedding", index_name="embedding_flat", index_type="FLAT", metric_type="COSINE")
    indexes.add_index(field_name="sparse", index_name="sparse_bm25", index_type="SPARSE_INVERTED_INDEX",
        metric_type="BM25", params={"inverted_index_algo": "DAAT_MAXSCORE", "bm25_k1": 1.2, "bm25_b": 0.75})
    client.create_collection(collection_name=name, schema=schema, index_params=indexes,
                             consistency_level="Strong", timeout=timeout)
    return validate_collection(client, corpus, model=model, timeout=timeout)
