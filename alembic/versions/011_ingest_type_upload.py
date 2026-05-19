"""Add 'upload' to documents.ingest_type ENUM.

Revision ID: 011
Revises: 009
Create Date: 2026-05-19

Note: there is no `010` revision in the Alembic chain — `migrations/010_feedback.sql`
is applied via `init_schema()` (schema.sql) only. This revision bridges from
`009` so `alembic upgrade head` widens the enum on deployments that follow
the alembic path; without it, `create_from_upload` would insert 'upload'
into a column that still allows only ('inline','file') and the multipart
endpoint would 500 on the first POST.
"""

from pathlib import Path

from alembic import op

revision = "011"
down_revision = "009"
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).parent.parent.parent / "migrations" / "011_ingest_type_upload.sql"
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
        "MODIFY COLUMN ingest_type ENUM('inline','file') NOT NULL DEFAULT 'inline', "
        "ALGORITHM=INSTANT"
    )
