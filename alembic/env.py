import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Load .env from project root if python-dotenv is available
_env_file = Path(__file__).parent.parent / ".env"
try:
    from dotenv import load_dotenv
    load_dotenv(_env_file, override=False)
except ImportError:
    pass


def run_migrations_offline() -> None:
    url = os.environ["MARIADB_DSN"]
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = os.environ["MARIADB_DSN"]
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
