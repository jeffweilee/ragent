"""T0.8b — schema.sql and alembic upgrade head must produce identical schemas."""

import os
import subprocess

import pytest

pytestmark = pytest.mark.docker


def _mysqldump(dsn: str) -> str:
    """Run mysqldump --no-data and return the normalised schema string."""
    # Extract host/port/user/password/db from DSN
    # Format: mysql+pymysql://user:pass@host:port/db?...
    from urllib.parse import urlparse

    parsed = urlparse(dsn.replace("mysql+pymysql://", "mysql://"))
    host = parsed.hostname or "127.0.0.1"
    db = parsed.path.lstrip("/")
    result = subprocess.run(
        [
            "mysqldump",
            "--no-data",
            "--skip-comments",
            "--column-statistics=0",  # MySQL 8.0 client compat with MariaDB 10.x
            "--protocol=TCP",  # force TCP, avoid Unix socket when host=localhost
            f"--ignore-table={db}.alembic_version",  # alembic tracking table not part of app schema
            "-h",
            host,
            "-P",
            str(parsed.port or 3306),
            f"-u{parsed.username}",
            f"-p{parsed.password}",
            db,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    # Normalise: remove AUTO_INCREMENT counters, timestamps, version comments
    import re

    schema = result.stdout
    schema = re.sub(r" AUTO_INCREMENT=\d+", "", schema)
    schema = re.sub(r"/\*![0-9]+ .*?\*/;?", "", schema, flags=re.DOTALL)
    return schema.strip()


def _apply_schema_sql(dsn: str) -> None:
    from pathlib import Path

    import sqlalchemy
    from sqlalchemy import text

    schema_sql = (Path(__file__).parents[2] / "migrations" / "schema.sql").read_text()
    engine = sqlalchemy.create_engine(dsn)
    with engine.connect() as conn:
        for stmt in schema_sql.split(";"):
            stmt = stmt.strip()
            if stmt and not stmt.startswith("--"):
                conn.execute(text(stmt))
        conn.commit()


def _apply_alembic(dsn: str) -> None:
    from pathlib import Path

    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        env={**os.environ, "MARIADB_DSN": dsn},
        cwd=str(Path(__file__).parents[2]),
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="module")
def schema_sql_dsn(mariadb_container) -> str:
    """Fresh MariaDB DB with schema applied via schema.sql."""
    from testcontainers.mysql import MySqlContainer

    with MySqlContainer(image="mariadb:10.6", username="u", password="p", dbname="schema_sql") as c:
        dsn = f"mysql+pymysql://u:p@{c.get_container_host_ip()}:{c.get_exposed_port(3306)}/schema_sql?charset=utf8mb4"
        _apply_schema_sql(dsn)
        yield dsn


@pytest.fixture(scope="module")
def alembic_dsn(mariadb_container) -> str:
    """Fresh MariaDB DB with schema applied via alembic upgrade head."""
    from testcontainers.mysql import MySqlContainer

    with MySqlContainer(image="mariadb:10.6", username="u", password="p", dbname="alembic_db") as c:
        dsn = f"mysql+pymysql://u:p@{c.get_container_host_ip()}:{c.get_exposed_port(3306)}/alembic_db?charset=utf8mb4"
        _apply_alembic(dsn)
        yield dsn


def test_schema_sql_equals_alembic_head(schema_sql_dsn: str, alembic_dsn: str) -> None:
    dump_a = _mysqldump(schema_sql_dsn)
    dump_b = _mysqldump(alembic_dsn)
    assert dump_a == dump_b, (
        "schema.sql and alembic upgrade head produce different schemas — "
        "update them in lockstep (spec §6.1 invariant)."
    )
