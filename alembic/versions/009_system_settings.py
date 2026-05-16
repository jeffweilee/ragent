"""Add system_settings table for runtime-mutable settings (B50, T-EM.5).

Revision ID: 009
Revises: 008
Create Date: 2026-05-15
"""

import json
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None

_DDL = (Path(__file__).parent.parent.parent / "migrations" / "009_system_settings.sql").read_text(
    encoding="utf-8"
)


def _strip_comments(fragment: str) -> str:
    lines = [ln for ln in fragment.splitlines() if not ln.strip().startswith("--")]
    return "\n".join(lines).strip()


# Seed rows live here (not in the .sql file) so JSON values can carry any
# character — including ';' — without colliding with naive split-by-semicolon
# parsing of the migration file. Stored as JSON strings, MariaDB validates
# them against the JSON column type.
_SEED_ROWS: list[tuple[str, object]] = [
    (
        "embedding.stable",
        {
            "name": "bge-m3",
            "dim": 1024,
            "api_url": "",
            "model_arg": "bge-m3",
            "field": "embedding_bgem3_1024",
        },
    ),
    ("embedding.candidate", None),
    ("embedding.read", "stable"),
    ("embedding.retired", []),
]


def upgrade() -> None:
    conn = op.get_bind()
    # DDL is one statement; execute as-is via the driver to avoid bind-param
    # interpretation of any `:` characters in comments.
    ddl = _strip_comments(_DDL)
    if ddl:
        conn.exec_driver_sql(ddl)

    # NOTE: MariaDB 10.6 does not accept `CAST(... AS JSON)` in INSERT VALUES;
    # the JSON column accepts any string the JSON_VALID constraint approves,
    # so we pass the JSON text directly.
    insert = text(
        "INSERT IGNORE INTO system_settings (setting_key, setting_value) VALUES (:key, :value)"
    )
    for key, value in _SEED_ROWS:
        conn.execute(insert, {"key": key, "value": json.dumps(value)})


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS system_settings")
