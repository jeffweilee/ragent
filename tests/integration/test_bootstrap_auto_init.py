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


def test_first_boot_creates_chunks_default_pipeline(mariadb_dsn: str, es_url: str) -> None:
    """T-EI.3 / B59 — `chunks_default` ingest pipeline MUST be present after
    auto_init; without it, every chunk write would fail because the index's
    `default_pipeline` setting references it."""
    auto_init(mariadb_dsn, es_url)
    from ragent.bootstrap.init_schema import _es_request

    result = _es_request(f"{es_url}/_ingest/pipeline/chunks_default")
    assert result is not None and "chunks_default" in result, (
        "chunks_default ingest pipeline should exist after auto_init"
    )


def test_chunk_write_populates_indexed_at_via_pipeline(mariadb_dsn: str, es_url: str) -> None:
    """T-EI.4 / B59 — proof that ES (not Python) fills `indexed_at`: write a
    chunk doc whose `_source` does NOT carry `indexed_at`, then GET it back
    and assert the field is present and parseable as ISO-8601."""
    import datetime as _dt

    auto_init(mariadb_dsn, es_url)
    from ragent.bootstrap.init_schema import _es_request

    chunk_id = "indexed-at-probe-001"
    payload = {
        "chunk_id": chunk_id,
        "document_id": "doc-indexed-at-probe",
        "text": "indexed_at probe body",
        # NOTE: intentionally NO `indexed_at` here — the ingest pipeline fills it.
    }
    _es_request(f"{es_url}/chunks_v1/_doc/{chunk_id}?refresh=true", method="PUT", body=payload)
    doc = _es_request(f"{es_url}/chunks_v1/_doc/{chunk_id}")

    indexed_at = doc["_source"].get("indexed_at")
    assert indexed_at is not None, (
        "`indexed_at` missing on chunk write — chunks_default pipeline not "
        "wired (check settings.index.default_pipeline)."
    )
    # ES emits ISO-8601 with `Z` suffix; normalize to UTC offset for fromisoformat.
    parsed = _dt.datetime.fromisoformat(indexed_at.replace("Z", "+00:00"))
    age = _dt.datetime.now(tz=_dt.UTC) - parsed
    assert age.total_seconds() < 60, (
        f"`indexed_at`={indexed_at} is older than 60s — clock skew or stale write?"
    )


def test_second_boot_is_noop(mariadb_dsn: str, es_url: str) -> None:
    """auto_init twice does not raise and does not alter existing schema."""
    auto_init(mariadb_dsn, es_url)
    auto_init(mariadb_dsn, es_url)  # second call — must not raise


def test_first_boot_creates_read_alias(mariadb_dsn: str, es_url: str) -> None:
    """T-EM-R.2 — after auto_init, `chunks_v1_active` alias points to `chunks_v1`."""
    auto_init(mariadb_dsn, es_url)
    from ragent.bootstrap.init_schema import _es_request

    result = _es_request(f"{es_url}/_alias/chunks_v1_active", method="HEAD")
    assert result is not None, (
        "chunks_v1_active alias should exist after auto_init — "
        "ActiveModelRegistry.read_alias routes reads through this alias"
    )


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
