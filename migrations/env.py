from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from ticketmind.core.config import get_settings
from ticketmind.db.base import Base
import ticketmind.tickets.models
import ticketmind.knowledge.models

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    """Read the database URL from TicketMind settings."""
    return get_settings().database_url.unicode_string()


def run_migrations_offline() -> None:
    """Generate migration SQL without opening a database connection."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured database."""
    provided_connection = config.attributes.get("connection")
    if provided_connection is not None:
        context.configure(connection=provided_connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = database_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
