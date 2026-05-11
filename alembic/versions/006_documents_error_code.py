"""Persist failure diagnostics (error_code + error_reason) on documents.

Revision ID: 006
Revises: 005
Create Date: 2026-05-09
"""

from pathlib import Path

from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).parent.parent.parent / "migrations" / "006_documents_error_code.sql"
).read_text(encoding="utf-8")


def _strip_comments(fragment: str) -> str:
    lines = [ln for ln in fragment.splitlines() if not ln.strip().startswith("--")]
    return "\n".join(lines).strip()


def upgrade() -> None:
    for raw in _SQL.split(";"):
        stmt = _strip_comments(raw)
        if stmt:
            op.execute(stmt)


def downgrade() -> None:
    op.execute(
        "ALTER TABLE documents "
        "DROP COLUMN error_code, "
        "DROP COLUMN error_reason, "
        "ALGORITHM=COPY, LOCK=SHARED"
    )
