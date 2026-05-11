"""Widen mime_type column from VARCHAR(64) to VARCHAR(128) for DOCX/PPTX MIMEs.

Revision ID: 007
Revises: 006
Create Date: 2026-05-12
"""

from pathlib import Path

from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None

_SQL = (Path(__file__).parent.parent.parent / "migrations" / "007_widen_mime_type.sql").read_text(
    encoding="utf-8"
)


def _strip_comments(fragment: str) -> str:
    lines = [ln for ln in fragment.splitlines() if not ln.strip().startswith("--")]
    return "\n".join(lines).strip()


def upgrade() -> None:
    for raw in _SQL.split(";"):
        stmt = _strip_comments(raw)
        if stmt:
            op.execute(stmt)


def downgrade() -> None:
    op.execute("ALTER TABLE documents MODIFY COLUMN mime_type VARCHAR(64) NULL, ALGORITHM=INSTANT")
