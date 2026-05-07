"""Documents: persist mime_type column for metrics + auditing.

Revision ID: 004
Revises: 003
Create Date: 2026-05-07
"""

from pathlib import Path

from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).parent.parent.parent / "migrations" / "004_documents_mime_type.sql"
).read_text()


def _strip_comments(fragment: str) -> str:
    lines = [ln for ln in fragment.splitlines() if not ln.strip().startswith("--")]
    return "\n".join(lines).strip()


def upgrade() -> None:
    for raw in _SQL.split(";"):
        stmt = _strip_comments(raw)
        if stmt:
            op.execute(stmt)


def downgrade() -> None:
    op.drop_column("documents", "mime_type")
