"""Add surrogate auto-increment id column to documents table.

Revision ID: 008
Revises: 007
Create Date: 2026-05-12
"""

from pathlib import Path

from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None

_SQL = (Path(__file__).parent.parent.parent / "migrations" / "008_documents_id.sql").read_text(
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
    op.execute(
        "ALTER TABLE documents DROP INDEX uq_document_id, DROP PRIMARY KEY, DROP COLUMN id, ADD PRIMARY KEY (document_id), ALGORITHM=COPY"  # noqa: E501
    )
