from types import SimpleNamespace

import pytest

from ticketmind.knowledge.index import MilvusKnowledgeIndex


def test_collection_validation_refreshes_budget_before_each_sdk_call():
    calls = []
    def budget():
        if calls:
            raise TimeoutError("run deadline reached")
        return 0.1
    def has_collection(**kwargs):
        calls.append("has_collection")
        assert kwargs["timeout"] == 0.1
        return True
    def forbidden(**kwargs):
        pytest.fail("SDK call started after the Agent deadline")
    index = MilvusKnowledgeIndex(SimpleNamespace(has_collection=has_collection, describe_collection=forbidden), budget)
    with pytest.raises(TimeoutError):
        index.validate(SimpleNamespace(collection_name="test"))
    assert calls == ["has_collection"]


def test_long_utf8_manifest_uses_stable_compact_description():
    import hashlib
    from ticketmind.knowledge.index import collection_description, manifest_text, production_manifest

    original = production_manifest()
    assert collection_description(original) == manifest_text(original)  # preserve existing collections
    large = {"schema_version": 1, "analyzer": {"tokenizer": {"dict": ["历史知识"] * 300}}}
    raw = manifest_text(large)
    assert len(raw.encode("utf-8")) > 1024
    compact = collection_description(large)
    assert compact == "manifest-sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert len(compact.encode("utf-8")) <= 1024
    assert collection_description(large) == compact  # deterministic across restarts


def test_long_manifest_create_and_validate_use_same_compact_description():
    from unittest.mock import MagicMock

    from ticketmind.knowledge.index import collection_description, MilvusKnowledgeIndex
    from ticketmind.retrieval.schemas import RetrievalError
    from pymilvus import FunctionType

    analyzer = {"tokenizer": {"type": "jieba", "dict": ["历史知识"] * 300, "mode": "search"},
                "filter": ["lowercase"]}
    manifest = {"schema_version": 1, "analyzer": analyzer, "corpus_version": "synthetic-test",
                "embedding_model": "text-embedding-v4", "dimension": 1024}
    dataset = SimpleNamespace(collection_name="synthetic-test-index", manifest=manifest)
    client = MagicMock()
    client.has_collection.return_value = False
    index = MilvusKnowledgeIndex(client, 10)
    # Only verify collection creation arguments here; validation is exercised with a real-shaped mock below.
    original_validate = index.validate
    index.validate = lambda value: None
    index.ensure(dataset)
    assert client.create_schema.call_args.kwargs["description"] == collection_description(manifest)
    assert len(client.create_schema.call_args.kwargs["description"].encode("utf-8")) <= 1024

    index.validate = original_validate
    client.has_collection.return_value = True
    client.describe_collection.return_value = {
        "description": collection_description(manifest),
        "fields": [
            {"name": "source_id"},
            {"name": "corpus_version"},
            {"name": "text"},
            {"name": "bm25_text", "params": {"analyzer_params": analyzer}},
            {"name": "embedding", "params": {"dim": 1024}},
            {"name": "sparse"},
        ],
        "functions": [{"type": FunctionType.BM25, "input_field_names": ["bm25_text"],
                       "output_field_names": ["sparse"]}],
    }
    client.describe_index.side_effect = [
        {"field_name": "embedding", "metric_type": "COSINE"},
        {"field_name": "sparse", "metric_type": "BM25"},
    ]
    index.validate(dataset)
    client.describe_collection.return_value["description"] = "manifest-sha256:" + "0" * 64
    with pytest.raises(RetrievalError) as error:
        index.validate(dataset)
    assert error.value.code == "collection_version_mismatch"
