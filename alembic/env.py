import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, inspect, pool, text

from ragent.bootstrap.init_schema import iter_statements

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False: stdlib's default (True) disables every
    # already-created logger not listed in alembic.ini's [loggers] section —
    # this silently kills app loggers (e.g. twp_ai.agents.*) created at
    # import time whenever a migration runs mid-process (e.g. in tests).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# ==============================================================================
# 中心化關聯維護區 — 嚴格規定 SQL 執行鏈結。
# 若有人在 Git 誤刪檔案、改錯名字或插隊，verify_and_get_chain() 會立刻熔斷。
# ==============================================================================
MIGRATION_CHAIN = [
    {"version": 1, "upgrade": "001_initial.sql", "downgrade": "001_initial.sql"},
    {"version": 2, "upgrade": "002_ingest_v2.sql", "downgrade": "002_ingest_v2.sql"},
    {"version": 3, "upgrade": "003_drop_chunks.sql", "downgrade": "003_drop_chunks.sql"},
    {
        "version": 4,
        "upgrade": "004_documents_mime_type.sql",
        "downgrade": "004_documents_mime_type.sql",
    },
    {
        "version": 5,
        "upgrade": "005_rename_source_workspace_to_source_meta.sql",
        "downgrade": "005_rename_source_workspace_to_source_meta.sql",
    },
    {
        "version": 6,
        "upgrade": "006_documents_error_code.sql",
        "downgrade": "006_documents_error_code.sql",
    },
    {"version": 7, "upgrade": "007_widen_mime_type.sql", "downgrade": "007_widen_mime_type.sql"},
    {"version": 8, "upgrade": "008_documents_id.sql", "downgrade": "008_documents_id.sql"},
    {"version": 9, "upgrade": "009_system_settings.sql", "downgrade": "009_system_settings.sql"},
    {"version": 10, "upgrade": "010_feedback.sql", "downgrade": "010_feedback.sql"},
    {
        "version": 11,
        "upgrade": "011_ingest_type_upload.sql",
        "downgrade": "011_ingest_type_upload.sql",
    },
    {
        "version": 12,
        "upgrade": "012_documents_status_created_index.sql",
        "downgrade": "012_documents_status_created_index.sql",
    },
    {"version": 13, "upgrade": "013_skills.sql", "downgrade": "013_skills.sql"},
    {"version": 14, "upgrade": "014_chat_attachments.sql", "downgrade": "014_chat_attachments.sql"},
    # 未來擴充直接於此追加，例如：
    # {"version": 15, "upgrade": "015_add_index.sql", "downgrade": "015_drop_index.sql"},
]

BASE_DIR = Path(__file__).resolve().parent
UPGRADE_DIR = BASE_DIR / "sql" / "upgrade"
DOWNGRADE_DIR = BASE_DIR / "sql" / "downgrade"


def _sync_dsn() -> str:
    # Alembic runs synchronously; coerce the async aiomysql driver to pymysql.
    # Mirrors src/ragent/bootstrap/init_schema.py:_to_sync_dsn (kept local to
    # avoid pulling that module's import chain into alembic bootstrap).
    return os.environ["MARIADB_DSN"].replace("mysql+aiomysql://", "mysql+pymysql://")


def get_current_db_version(connection) -> int:
    """從資料庫取得目前版號，若無 tracking 表則自動建立。

    Two pre-chain legacy states must resolve to "already at head" instead of
    "version 0", or a later `upgrade head` would replay DDL against tables
    that already exist:
    - a `version_num = 'squash'` row left by the deleted single-revision
      `alembic/versions/000_squash.py`;
    - no tracking row at all because the DB was bootstrapped directly from
      `migrations/schema.sql` (boot auto-init, B3), which never wrote one.
    """
    connection.execute(
        text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
    )
    result = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    if result is not None:
        if result.isdigit():
            return int(result)
        if result == "squash":
            return len(MIGRATION_CHAIN)
        raise ValueError(f"alembic_version.version_num has unexpected value: {result!r}")

    has_schema = inspect(connection).has_table("documents")
    return len(MIGRATION_CHAIN) if has_schema else 0


def update_db_version(connection, version: int) -> None:
    """更新資料庫中的最新版本追蹤指標（參數化查詢，不做字串拼接）。"""
    connection.execute(text("DELETE FROM alembic_version"))
    if version > 0:
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:v)").bindparams(
                v=f"{version:03d}"
            )
        )


def verify_and_get_chain() -> list[dict]:
    """安全核心：自動防呆熔斷器。檢查宣告與實體檔案是否完整，若異常則拒絕施工。"""
    verified_chain = []
    expected_version = 1

    for item in MIGRATION_CHAIN:
        v = item["version"]
        if v != expected_version:
            raise ValueError(f"MIGRATION_CHAIN 版本號未連續！預期是 {expected_version} 但收到 {v}")

        up_path = UPGRADE_DIR / item["upgrade"]
        down_path = DOWNGRADE_DIR / item["downgrade"]

        if not up_path.exists():
            raise FileNotFoundError(f"找不到升級 SQL: {up_path}")
        if not down_path.exists():
            raise FileNotFoundError(f"找不到降級 SQL: {down_path}")

        verified_chain.append(
            {
                "version": v,
                "up_path": up_path,
                "down_path": down_path,
                "up_name": item["upgrade"],
                "down_name": item["downgrade"],
            }
        )
        expected_version += 1

    return verified_chain


def _apply_sql_file(connection, path: str) -> None:
    sql = Path(path).read_text(encoding="utf-8")
    for stmt in iter_statements(sql):
        connection.execute(text(stmt))


def run_migrations_offline() -> None:
    context.configure(url=_sync_dsn(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _is_upgrade_target(target: str | None, current_v: int) -> bool:
    if (
        target is None
        or target in ("head", "up")
        or (isinstance(target, str) and target.startswith("+"))
    ):
        return True
    if isinstance(target, str) and target.isdigit():
        # Bare version number (e.g. "alembic upgrade 012") — direction
        # depends on where the DB currently is, not on the string's shape.
        return int(target) >= current_v
    return False


def _upgrade_target_version(target: str | None, current_v: int, max_v: int) -> int:
    if target is None or target == "head":
        return max_v
    if isinstance(target, str) and target.isdigit():
        return min(int(target), max_v)
    steps = int(target.replace("+", "")) if target.startswith("+") else max_v
    return min(current_v + steps, max_v)


def _downgrade_target_version(target: str, current_v: int) -> int:
    if target == "base":
        return 0
    if target.isdigit():
        return max(int(target), 0)
    steps = int(target.replace("-", ""))
    return max(current_v - steps, 0)


def _run_chain(connection, target: str | None) -> None:
    chain = verify_and_get_chain()
    max_available_v = len(chain)
    current_v = get_current_db_version(connection)
    # get_current_db_version() autobegins a transaction on first execute;
    # commit it so the explicit connection.begin() below doesn't collide
    # with SQLAlchemy's implicit one.
    connection.commit()

    with connection.begin():
        if _is_upgrade_target(target, current_v):
            target_v = _upgrade_target_version(target, current_v, max_available_v)
            if current_v >= target_v:
                return

            for item in chain:
                v = item["version"]
                if current_v < v <= target_v:
                    _apply_sql_file(connection, item["up_path"])
                    current_v = v
                    update_db_version(connection, current_v)

        else:
            target_v = _downgrade_target_version(target, current_v)
            if current_v <= target_v:
                return

            for item in reversed(chain):
                v = item["version"]
                if target_v < v <= current_v:
                    _apply_sql_file(connection, item["down_path"])
                    update_db_version(connection, v - 1)
                    current_v = v - 1


def _raw_destination_rev() -> str | None:
    """Raw ``upgrade``/``downgrade`` destination argument, e.g. "head",
    "base", "+2", "-1" — unlike ``context.get_revision_argument()``, this is
    NOT run through Alembic's revision-script resolution, which collapses
    both "head" and "base" to ``None`` when there are no revision scripts
    (this project's chain lives in alembic/sql/, not alembic/versions/) and
    would make upgrade/downgrade indistinguishable.
    """
    return context._proxy.context_opts.get("destination_rev")


def run_migrations_online() -> None:
    target = _raw_destination_rev()
    if not isinstance(target, str):
        # Commands other than upgrade/downgrade (e.g. `alembic current`,
        # `alembic stamp`) don't pass a plain string destination — this
        # hand-rolled chain only knows how to replay upgrade/downgrade SQL,
        # so it no-ops rather than misapplying DDL or crashing.
        return

    connectable = create_engine(_sync_dsn(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        _run_chain(connection, target)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
