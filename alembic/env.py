import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False: stdlib's default (True) disables every
    # already-created logger not listed in alembic.ini's [loggers] section —
    # this silently kills app loggers (e.g. twp_ai.agents.*) created at
    # import time whenever a migration runs mid-process (e.g. in tests).
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _sync_dsn() -> str:
    # Alembic runs synchronously; coerce the async aiomysql driver to pymysql.
    # Mirrors src/ragent/bootstrap/init_schema.py:_to_sync_dsn (kept local to
    # avoid pulling that module's import chain into alembic bootstrap).
    return os.environ["MARIADB_DSN"].replace("mysql+aiomysql://", "mysql+pymysql://")


def run_migrations_offline() -> None:
    context.configure(url=_sync_dsn(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_sync_dsn(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
