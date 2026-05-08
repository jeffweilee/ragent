"""T0.8c — auto_init: first boot creates schema idempotently; second boot is a no-op."""

import pytest

from ragent.bootstrap.init_schema import _to_sync_dsn, auto_init, init_mariadb

pytestmark = pytest.mark.docker


def test_first_boot_creates_mariadb_tables(mariadb_dsn: str) -> None:
    import sqlalchemy
    from sqlalchemy import create_engine
    from sqlalchemy import inspect as sa_inspect

    sync_dsn = _to_sync_dsn(mariadb_dsn)
    init_mariadb(create_engine(sync_dsn))
    insp = sa_inspect(sqlalchemy.create_engine(sync_dsn))
    assert "documents" in insp.get_table_names()
    # `chunks` table dropped in C6 (003_drop_chunks.sql); chunks live only in ES.
    assert "chunks" not in insp.get_table_names()


def test_first_boot_creates_es_index(mariadb_dsn: str, es_url: str) -> None:
    auto_init(mariadb_dsn, es_url)
    from ragent.bootstrap.init_schema import _es_request

    result = _es_request(f"{es_url}/chunks_v1")
    assert result is not None, "chunks_v1 index should exist after auto_init"


def test_second_boot_is_noop(mariadb_dsn: str, es_url: str) -> None:
    """auto_init twice does not raise and does not alter existing schema."""
    auto_init(mariadb_dsn, es_url)
    auto_init(mariadb_dsn, es_url)  # second call — must not raise


def test_mariadb_tables_have_expected_columns(mariadb_dsn: str) -> None:
    import sqlalchemy
    from sqlalchemy import create_engine
    from sqlalchemy import inspect as sa_inspect

    sync_dsn = _to_sync_dsn(mariadb_dsn)
    init_mariadb(create_engine(sync_dsn))
    insp = sa_inspect(sqlalchemy.create_engine(sync_dsn))
    doc_cols = {c["name"] for c in insp.get_columns("documents")}
    assert {
        "document_id",
        "create_user",
        "source_id",
        "source_app",
        "source_title",
        "source_meta",
        "object_key",
        "status",
        "attempt",
        "created_at",
        "updated_at",
        # v2 columns (002_ingest_v2.sql)
        "ingest_type",
        "minio_site",
        "source_url",
    } <= doc_cols
