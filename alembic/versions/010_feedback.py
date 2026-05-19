"""Add feedback table for per-source ranking signal (T-FB.3, B54/B55).

Revision ID: 010
Revises: 009
Create Date: 2026-05-19
"""

from pathlib import Path

from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None

_SQL = (Path(__file__).parent.parent.parent / "migrations" / "010_feedback.sql").read_text(
    encoding="utf-8"
)


def _strip_comments(fragment: str) -> str:
    lines = [ln for ln in fragment.splitlines() if not ln.strip().startswith("--")]
    return "\n".join(lines).strip()


def upgrade() -> None:
    """Single source of truth: `migrations/010_feedback.sql`. Mirrors the
    009 wrapper pattern — `exec_driver_sql` per statement, comments stripped
    so the SQL file remains the canonical DDL also loaded by `schema.sql`."""
    conn = op.get_bind()
    for raw in _SQL.split(";"):
        stmt = _strip_comments(raw)
        if stmt:
            conn.exec_driver_sql(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS feedback")
