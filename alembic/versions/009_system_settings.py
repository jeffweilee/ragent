"""Add system_settings table for runtime-mutable settings (B50, T-EM.5).

Revision ID: 009
Revises: 008
Create Date: 2026-05-15
"""

from pathlib import Path

from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None

_SQL = (Path(__file__).parent.parent.parent / "migrations" / "009_system_settings.sql").read_text(
    encoding="utf-8"
)


def _strip_comments(fragment: str) -> str:
    lines = [ln for ln in fragment.splitlines() if not ln.strip().startswith("--")]
    return "\n".join(lines).strip()


def upgrade() -> None:
    """Single source of truth for both CREATE TABLE and the seed INSERTs:
    `migrations/009_system_settings.sql`. `exec_driver_sql` bypasses
    SQLAlchemy's text() bind-param parser so the JSON_OBJECT seed payload
    is sent verbatim. `init_mariadb` runs the same SQL via `schema.sql`."""
    conn = op.get_bind()
    for raw in _SQL.split(";"):
        stmt = _strip_comments(raw)
        if stmt:
            conn.exec_driver_sql(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS system_settings")
