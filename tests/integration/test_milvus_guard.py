"""Offline proof that Milvus test writes are denied before delegation."""
from uuid import uuid4

import pytest

from milvus_guard import GuardedMilvusClient


class RecordingClient:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append((name, kwargs.get("collection_name")))
            return None
        return call


def test_guard_rejects_any_nonowned_mutation_before_client_call():
    dense = f"tm_test_{uuid4().hex}_knowledge"
    bm25 = dense.replace("_knowledge", "_bm25")
    client = RecordingClient()
    guard = GuardedMilvusClient(client, {dense, bm25})

    for operation in ("create_collection", "upsert", "insert", "delete", "drop_collection"):
        with pytest.raises(PermissionError):
            getattr(guard, operation)(collection_name="knowledge_production", ids=["x"])
        with pytest.raises(PermissionError):
            getattr(guard, operation)(collection_name="tm_test_other_bm25", ids=["x"])
    with pytest.raises(PermissionError):
        guard.drop_collection(collection_name=dense)
    with pytest.raises(PermissionError):
        guard.rename_collection(collection_name=dense)
    assert client.calls == []

    guard.create_collection(collection_name=dense)
    guard.upsert(collection_name=dense)
    guard.create_collection(collection_name=bm25)
    guard.delete(collection_name=bm25)
    guard.drop_collection(collection_name=dense)
    assert client.calls == [("create_collection", dense), ("upsert", dense),
                            ("create_collection", bm25), ("delete", bm25),
                            ("drop_collection", dense)]


def test_readonly_guard_for_frozen_collection_rejects_all_writes():
    client = RecordingClient()
    guard = GuardedMilvusClient(client)
    guard.has_collection(collection_name="knowledge_frozen")
    for operation in ("create_collection", "upsert", "delete", "drop_collection"):
        with pytest.raises(PermissionError):
            getattr(guard, operation)(collection_name="knowledge_frozen")
    assert client.calls == [("has_collection", "knowledge_frozen")]
