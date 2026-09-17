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
