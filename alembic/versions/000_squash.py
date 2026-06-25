"""Squashed migration: full schema in one step.

Revision ID: squash
Revises:
Create Date: 2026-05-20

This revision reads migrations/schema.sql at runtime so Alembic and the
bootstrap snapshot cannot drift into two separate DDL definitions.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from alembic import op

revision = "squash"
down_revision = None
branch_labels = None
depends_on = None

_SCHEMA_SQL = Path(__file__).resolve().parents[2] / "migrations" / "schema.sql"


def _strip_comments(sql: str) -> str:
    out: list[str] = []
    for line in sql.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("--"):
            continue
        idx = line.find("--")
        if idx >= 0:
            line = line[:idx].rstrip()
        out.append(line)
    return "\n".join(out).strip()


def _iter_statements(sql: str) -> Iterator[str]:
    for raw in _strip_comments(sql).split(";"):
        stmt = raw.strip()
        if stmt:
            yield stmt


def upgrade() -> None:
    conn = op.get_bind()
    for stmt in _iter_statements(_SCHEMA_SQL.read_text(encoding="utf-8")):
        conn.exec_driver_sql(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chat_attachment_artifacts")
    op.execute("DROP TABLE IF EXISTS chat_attachments")
    op.execute("DROP TABLE IF EXISTS feedback")
    op.execute("DROP TABLE IF EXISTS system_settings")
    op.execute("DROP TABLE IF EXISTS documents")
