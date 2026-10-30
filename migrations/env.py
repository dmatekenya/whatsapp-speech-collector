from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from src.config import get_settings
from src.storage.models import Base

config = context.config

# Keep loggers the app already created (e.g. when tests run migrations).
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# The URL comes from Settings (.env), never from alembic.ini.
database_url = get_settings().sync_database_url


# ============================================================
# Offline mode (emit SQL)
# ============================================================
def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# ============================================================
# Online mode (run against the database)
# ============================================================
def run_migrations_online() -> None:
    connectable = create_engine(database_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
