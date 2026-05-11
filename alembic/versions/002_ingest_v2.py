"""Ingest API v2: add ingest_type / minio_site / source_url columns.

Revision ID: 002
Revises: 001
Create Date: 2026-05-06

The chunks table is retained at this commit; v1 pipeline still writes
to it. C6 will DROP TABLE chunks once the v2 pipeline (C4) is live.
"""

from pathlib import Path

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None

_SQL = (Path(__file__).parent.parent.parent / "migrations" / "002_ingest_v2.sql").read_text(encoding="utf-8")


def _strip_comments(fragment: str) -> str:
    lines = [ln for ln in fragment.splitlines() if not ln.strip().startswith("--")]
    return "\n".join(lines).strip()


def upgrade() -> None:
    for raw in _SQL.split(";"):
        stmt = _strip_comments(raw)
        if stmt:
            op.execute(stmt)


def downgrade() -> None:
    op.drop_column("documents", "source_url")
    op.drop_column("documents", "minio_site")
    op.drop_column("documents", "ingest_type")
