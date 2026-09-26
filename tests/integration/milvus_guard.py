"""Fail closed before network IO when integration tests try to mutate Milvus."""
import re


_OWNED_NAME = re.compile(r"tm_test_[0-9a-f]{32}_(?:knowledge|bm25)\Z")
_READS = {"has_collection", "describe_collection", "describe_index", "get", "query",
          "search", "list_collections"}
_BUILDERS = {"create_schema", "prepare_index_params"}
_WRITES = {"create_collection", "drop_collection", "upsert", "insert", "delete"}


class GuardedMilvusClient:
    def __init__(self, client, owned_names=()):
        names = frozenset(owned_names)
        if any(_OWNED_NAME.fullmatch(name) is None for name in names):
            raise ValueError("Milvus test collection name must belong to a UUID test schema")
        self._client = client
        self.owned_names = names
        self.creation_attempts = set()

    def __getattr__(self, operation):
        if operation in _READS | _BUILDERS | {"close"}:
            return getattr(self._client, operation)
        if operation not in _WRITES:
            raise PermissionError(f"Milvus test operation is not allowlisted: {operation}")

        def guarded(*args, **kwargs):
            name = kwargs.get("collection_name")
            if args or name not in self.owned_names:
                raise PermissionError("Milvus mutation refused outside this test's UUID collections")
            if operation == "create_collection":
                self.creation_attempts.add(name)
            elif operation == "drop_collection" and name not in self.creation_attempts:
                raise PermissionError("Milvus test may drop only collections it attempted to create")
            return getattr(self._client, operation)(**kwargs)

        return guarded
