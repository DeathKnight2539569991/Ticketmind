from faulthandler import enable

from pymilvus import DataType,MilvusClient
CASE_COLLECTION="historical_cases_v1"
EMBEDDING_DIMENSION=1024
SOURCE_ID_MAX_BYTES=128
TEXT_MAX_BYTES=16384
VECTOR_INDEX="embedding_flat"
def create_case_collection(client: MilvusClient,*,timeout:float):
    """Create the case collection in Milvus."""
    schema=client.create_schema(
        auto_id=False,
        enable_dynamic_field=False,
    )
    schema.add_field(
        field_name="source_id",
        datatype=DataType.VARCHAR,
        is_primary=True,
        max_length=SOURCE_ID_MAX_BYTES,
    )
    schema.add_field(
        field_name="text",
        datatype=DataType.VARCHAR,
        max_length=TEXT_MAX_BYTES,
    )
    schema.add_field(
        field_name="embedding",
        datatype=DataType.FLOAT_VECTOR,
        dim=EMBEDDING_DIMENSION,
    )
    index_params=client.prepare_index_params()
    index_params.add_index(
        field_name="embedding",
        index_name=VECTOR_INDEX,
        index_type="FLAT",
        metric_type="COSINE"
    )
    client.create_collection(
        collection_name=CASE_COLLECTION,
        schema=schema,
        index_params=index_params,
        consistency_level="Strong",
        timeout=timeout,
    )