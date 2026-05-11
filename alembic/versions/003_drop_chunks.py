"""Drop chunks table (v2 cleanup).

Revision ID: 003
Revises: 002
Create Date: 2026-05-07

Chunks live exclusively in ES ``chunks_v1`` after C4. The MariaDB
``chunks`` table was last written by the v1 pipeline and is no longer
referenced. ``ChunkRepository`` is removed alongside this migration.
"""

from pathlib import Path

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None

_SQL = (Path(__file__).parent.parent.parent / "migrations" / "003_drop_chunks.sql").read_text(
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
        """
        CREATE TABLE chunks (
          chunk_id    CHAR(26)   NOT NULL,
          document_id CHAR(26)   NOT NULL,
          ord         INT        NOT NULL,
          text        MEDIUMTEXT NOT NULL,
          lang        VARCHAR(8) NOT NULL,
          PRIMARY KEY (chunk_id),
          INDEX idx_document (document_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
