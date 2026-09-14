"""Explicit integration-only helper: migrate a fresh PostgreSQL schema, never public."""
import re
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from ticketmind.core.config import Settings


@contextmanager
def isolated_database(url: str | None = None, *, revision="head"):
    url = url or Settings().database_url.unicode_string()
    schema = "tm_test_" + uuid4().hex
    if not re.fullmatch(r"tm_test_[0-9a-f]{32}", schema):
        raise ValueError("非法测试 schema 名称")
    owner = create_engine(url, connect_args={"connect_timeout": 5})
    with owner.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 5})
    try:
        root = Path(__file__).resolve().parents[3]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, revision)
        yield engine, sessionmaker(bind=engine, expire_on_commit=False, autoflush=False), schema
    finally:
        engine.dispose()
        # Only the UUID schema created above is eligible for cleanup.
        with owner.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        owner.dispose()
