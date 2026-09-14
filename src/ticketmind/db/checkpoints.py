"""Own checkpoint resources and a database-scoped single-instance startup lease."""
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


@contextmanager
def checkpoint_resources(engine):
    # Reuse the bound SQLAlchemy engine's actual connection arguments, including
    # isolated test schema options, without printing connection strings.
    args, kwargs = engine.dialect.create_connect_args(engine.url)
    # do_connect includes connect_args supplied to create_engine (e.g. search_path).
    with engine.connect() as connection:
        schema = connection.exec_driver_sql("SELECT current_schema()").scalar_one()
    kwargs.update(autocommit=True, prepare_threshold=0, row_factory=dict_row,
                  connect_timeout=5, options=f'-csearch_path={schema} -cstatement_timeout=15000')
    with Connection.connect(*args, **kwargs) as lease:
        key = lease.execute("SELECT hashtextextended(current_database() || ':' || current_schema() || ':ticketmind-api', 0) AS key").fetchone()["key"]
        if not lease.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (key,)).fetchone()["acquired"]:
            raise RuntimeError("同一数据库/schema 已有 TicketMind 实例；只支持单实例单 worker")
        try:
            with ConnectionPool(conninfo=args[0] if args else "", kwargs=kwargs, min_size=1, max_size=4,
                                timeout=10, open=True) as pool:
                pool.wait(timeout=10)
                saver = PostgresSaver(pool)
                saver.setup()
                yield saver
        finally:
            lease.execute("SELECT pg_advisory_unlock(%s)", (key,))
