"""Rename source_workspace -> source_meta and widen to VARCHAR(1024).

Revision ID: 005
Revises: 004
Create Date: 2026-05-07
"""

from pathlib import Path

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).parent.parent.parent
    / "migrations"
    / "005_rename_source_workspace_to_source_meta.sql"
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
        "CHANGE COLUMN source_meta source_workspace VARCHAR(64) NULL, "
        "ALGORITHM=COPY, LOCK=SHARED"
    )
