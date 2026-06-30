"""Pure-logic guards for alembic/env.py's hand-rolled migration chain.

verify_and_get_chain() is the fuse-breaker that refuses to run if
MIGRATION_CHAIN's version numbering or declared SQL files drift from disk —
these tests catch that without needing a live DB. The target-resolution
helpers (_is_upgrade_target / _upgrade_target_version /
_downgrade_target_version) are pure functions driving alembic's
head/up/+N/base/-N/-1 CLI semantics and are likewise covered without I/O.
"""

from __future__ import annotations

import importlib.util
import types
from contextlib import nullcontext
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ENV_PY = Path(__file__).resolve().parents[2] / "alembic" / "env.py"


def _load_env_module(monkeypatch):
    import alembic

    noop = lambda *a, **kw: None  # noqa: E731
    stub = types.SimpleNamespace(
        config=types.SimpleNamespace(config_file_name=None),
        is_offline_mode=lambda: True,
        configure=noop,
        run_migrations=noop,
        begin_transaction=nullcontext,
    )
    monkeypatch.setattr(alembic, "context", stub)
    monkeypatch.setenv("MARIADB_DSN", "mysql+aiomysql://u:p@h:3306/db")
    spec = importlib.util.spec_from_file_location("_alembic_env_under_test", ENV_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(monkeypatch):
    return _load_env_module(monkeypatch)


@pytest.fixture
def sqlite_conn():
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        yield conn


def test_verify_and_get_chain_matches_disk(env):
    chain = env.verify_and_get_chain()

    assert [item["version"] for item in chain] == list(range(1, len(env.MIGRATION_CHAIN) + 1))
    for item in chain:
        assert Path(item["up_path"]).exists()
        assert Path(item["down_path"]).exists()


def test_verify_and_get_chain_raises_on_gap(env, monkeypatch):
    monkeypatch.setattr(
        env,
        "MIGRATION_CHAIN",
        [
            {"version": 1, "upgrade": "001_initial.sql", "downgrade": "001_initial.sql"},
            {"version": 3, "upgrade": "003_drop_chunks.sql", "downgrade": "003_drop_chunks.sql"},
        ],
    )

    with pytest.raises(ValueError, match="版本號未連續"):
        env.verify_and_get_chain()


def test_verify_and_get_chain_raises_on_missing_file(env, monkeypatch):
    monkeypatch.setattr(
        env,
        "MIGRATION_CHAIN",
        [{"version": 1, "upgrade": "999_does_not_exist.sql", "downgrade": "001_initial.sql"}],
    )

    with pytest.raises(FileNotFoundError, match="找不到升級 SQL"):
        env.verify_and_get_chain()


@pytest.mark.parametrize(
    ("target", "current_v", "expected"),
    [
        (None, 3, True),
        ("head", 3, True),
        ("up", 3, True),
        ("+2", 3, True),
        ("-1", 3, False),
        ("base", 3, False),
        ("012", 3, True),
        ("012", 14, False),
        ("003", 3, True),
    ],
)
def test_is_upgrade_target(env, target, current_v, expected):
    assert env._is_upgrade_target(target, current_v) is expected


@pytest.mark.parametrize(
    ("target", "current_v", "max_v", "expected"),
    [
        ("head", 3, 14, 14),
        (None, 3, 14, 14),
        ("+2", 3, 14, 5),
        ("+99", 3, 14, 14),
        ("012", 3, 14, 12),
        ("099", 3, 14, 14),
    ],
)
def test_upgrade_target_version(env, target, current_v, max_v, expected):
    assert env._upgrade_target_version(target, current_v, max_v) == expected


@pytest.mark.parametrize(
    ("target", "current_v", "expected"),
    [
        ("base", 5, 0),
        ("-1", 5, 4),
        ("-3", 5, 2),
        ("-99", 5, 0),
        ("002", 5, 2),
    ],
)
def test_downgrade_target_version(env, target, current_v, expected):
    assert env._downgrade_target_version(target, current_v) == expected


def test_get_and_update_db_version_round_trip(env, sqlite_conn):
    conn = sqlite_conn
    assert env.get_current_db_version(conn) == 0

    env.update_db_version(conn, 7)
    assert env.get_current_db_version(conn) == 7

    env.update_db_version(conn, 0)
    assert env.get_current_db_version(conn) == 0


def test_get_current_db_version_squash_marker_resolves_to_head(env, sqlite_conn):
    conn = sqlite_conn
    conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
    conn.execute(text("INSERT INTO alembic_version VALUES ('squash')"))
    assert env.get_current_db_version(conn) == len(env.MIGRATION_CHAIN)


def test_get_current_db_version_garbage_value_raises(env, sqlite_conn):
    conn = sqlite_conn
    conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
    conn.execute(text("INSERT INTO alembic_version VALUES ('garbage')"))
    with pytest.raises(ValueError, match="unexpected value"):
        env.get_current_db_version(conn)


def test_get_current_db_version_no_row_but_schema_exists_resolves_to_head(env, sqlite_conn):
    conn = sqlite_conn
    conn.execute(text("CREATE TABLE documents (id INTEGER PRIMARY KEY)"))
    assert env.get_current_db_version(conn) == len(env.MIGRATION_CHAIN)


@pytest.mark.parametrize(
    ("destination_rev", "expected"),
    [("head", "head"), ("base", "base"), (None, None)],
)
def test_raw_destination_rev_distinguishes_head_and_base(
    env, monkeypatch, destination_rev, expected
):
    proxy = types.SimpleNamespace(context_opts={"destination_rev": destination_rev})
    monkeypatch.setattr(env.context, "_proxy", proxy, raising=False)
    assert env._raw_destination_rev() == expected


def test_run_migrations_online_noop_for_non_string_target(env, monkeypatch):
    """`alembic current`/`stamp` pass a non-string destination_rev; this chain
    only knows how to replay upgrade/downgrade SQL, so it must no-op without
    ever opening a DB connection."""
    monkeypatch.setattr(env, "_raw_destination_rev", lambda: None)

    def _fail_if_called(*_a, **_kw):
        raise AssertionError("create_engine must not be called on the no-op path")

    monkeypatch.setattr(env, "create_engine", _fail_if_called)
    env.run_migrations_online()
