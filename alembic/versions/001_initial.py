"""Initial schema: documents + chunks tables.

Revision ID: 001
Revises:
Create Date: 2026-05-04
"""

from pathlib import Path

from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None

_SQL = (Path(__file__).parent.parent.parent / "migrations" / "001_initial.sql").read_text()


def _strip_comments(fragment: str) -> str:
    lines = [ln for ln in fragment.splitlines() if not ln.strip().startswith("--")]
    return "\n".join(lines).strip()


def upgrade() -> None:
    for raw in _SQL.split(";"):
        stmt = _strip_comments(raw)
        if stmt:
            op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chunks")
    op.execute("DROP TABLE IF EXISTS documents")
