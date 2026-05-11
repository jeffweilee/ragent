"""T0.8b — schema.sql and alembic upgrade head must produce identical schemas."""

import re
from pathlib import Path

import pytest

from ragent.bootstrap.init_schema import _strip_comments
from tc_utils import tc_image

pytestmark = [pytest.mark.docker]


def _dump_schema(dsn: str) -> str:
    # Plain tables only — extend with SHOW CREATE VIEW / SHOW TRIGGERS if the
    # schema ever adds views, triggers, or routines.
    import sqlalchemy
    from sqlalchemy import text

    engine = sqlalchemy.create_engine(dsn)
    try:
        with engine.connect() as conn:
            tables = sorted(
                row[0]
                for row in conn.execute(text("SHOW TABLES")).fetchall()
                if row[0] != "alembic_version"  # tracking table, not part of app schema
            )
            ddls = [
                conn.execute(text(f"SHOW CREATE TABLE `{table}`")).fetchone()[1] for table in tables
            ]
    finally:
        engine.dispose()
    schema = "\n\n".join(ddls)
    schema = re.sub(r" AUTO_INCREMENT=\d+", "", schema)
    return schema.strip()


def _apply_schema_sql(dsn: str) -> None:
    from pathlib import Path

    import sqlalchemy
    from sqlalchemy import text

    schema_sql = (Path(__file__).parents[2] / "migrations" / "schema.sql").read_text(
        encoding="utf-8"
    )
    engine = sqlalchemy.create_engine(dsn)
    with engine.begin() as conn:
        for raw in schema_sql.split(";"):
            stmt = _strip_comments(raw)
            if stmt:
                conn.execute(text(stmt))


def _apply_alembic(dsn: str) -> None:
    import os

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    # alembic/env.py reads MARIADB_DSN from os.environ; set it for this process.
    old = os.environ.get("MARIADB_DSN")
    os.environ["MARIADB_DSN"] = dsn
    try:
        command.upgrade(cfg, "head")
    finally:
        if old is None:
            os.environ.pop("MARIADB_DSN", None)
        else:
            os.environ["MARIADB_DSN"] = old


@pytest.fixture(scope="module")
def schema_sql_dsn(mariadb_container) -> str:
    """Fresh MariaDB DB with schema applied via schema.sql."""
    from testcontainers.mysql import MySqlContainer

    with MySqlContainer(
        image=tc_image("mariadb:10.6"), username="u", password="p", dbname="schema_sql"
    ) as c:
        dsn = f"mysql+pymysql://u:p@{c.get_container_host_ip()}:{c.get_exposed_port(3306)}/schema_sql?charset=utf8mb4"
        _apply_schema_sql(dsn)
        yield dsn


@pytest.fixture(scope="module")
def alembic_dsn(mariadb_container) -> str:
    """Fresh MariaDB DB with schema applied via alembic upgrade head."""
    from testcontainers.mysql import MySqlContainer

    with MySqlContainer(
        image=tc_image("mariadb:10.6"), username="u", password="p", dbname="alembic_db"
    ) as c:
        dsn = f"mysql+pymysql://u:p@{c.get_container_host_ip()}:{c.get_exposed_port(3306)}/alembic_db?charset=utf8mb4"
        _apply_alembic(dsn)
        yield dsn


def test_schema_sql_equals_alembic_head(schema_sql_dsn: str, alembic_dsn: str) -> None:
    dump_a = _dump_schema(schema_sql_dsn)
    dump_b = _dump_schema(alembic_dsn)
    assert dump_a == dump_b, (
        "schema.sql and alembic upgrade head produce different schemas — "
        "update them in lockstep (spec §6.1 invariant)."
    )
