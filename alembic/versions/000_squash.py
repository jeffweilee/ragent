"""Squashed migration: full schema in one step (consolidates 001–012).

Revision ID: squash
Revises:
Create Date: 2026-05-20

This file is a direct consolidation of migrations 001..012 and does not
read migrations/schema.sql at runtime.
"""

from collections.abc import Iterator

from alembic import op

revision = "squash"
down_revision = None
branch_labels = None
depends_on = None

_SQL = """
CREATE TABLE IF NOT EXISTS documents (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  document_id      CHAR(26)     NOT NULL,
  create_user      VARCHAR(64)  NOT NULL,
  source_id        VARCHAR(128) NOT NULL,
  source_app       VARCHAR(64)  NOT NULL,
  source_title     VARCHAR(256) NOT NULL,
  source_meta      VARCHAR(1024) NULL,
  object_key       VARCHAR(256) NOT NULL,
  status           ENUM('UPLOADED','PENDING','READY','FAILED','DELETING') NOT NULL,
  attempt          INT          NOT NULL DEFAULT 0,
  created_at       DATETIME(6)  NOT NULL,
  updated_at       DATETIME(6)  NOT NULL,
  ingest_type      ENUM('inline','file','upload') NOT NULL DEFAULT 'inline',
  minio_site       VARCHAR(64)  NULL,
  source_url       VARCHAR(2048) NULL,
  mime_type        VARCHAR(128) NULL,
  error_code       VARCHAR(64)  NULL,
  error_reason     VARCHAR(255) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_document_id (document_id),
  INDEX idx_status_updated (status, updated_at),
  INDEX idx_source_app_id_status_created (source_app, source_id, status, created_at),
  INDEX idx_create_user_document (create_user, document_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS system_settings (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  setting_key   VARCHAR(64) NOT NULL,
  setting_value JSON NOT NULL,
  updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_setting_key (setting_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO system_settings (setting_key, setting_value) VALUES
  (
    'embedding.stable',
    JSON_OBJECT(
      'name','bge-m3','dim',1024,'api_url','',
      'model_arg','bge-m3','field','embedding_bgem3_1024'
    )
  ),
  ('embedding.candidate', 'null'),
  ('embedding.read',      JSON_QUOTE('stable')),
  ('embedding.retired',   JSON_ARRAY());

CREATE TABLE IF NOT EXISTS feedback (
  feedback_id     CHAR(26)     PRIMARY KEY,
  request_id      CHAR(26)     NOT NULL,
  user_id         VARCHAR(64)  NOT NULL,
  source_app      VARCHAR(64)  NOT NULL,
  source_id       VARCHAR(128) NOT NULL,
  vote            TINYINT      NOT NULL,
  reason          VARCHAR(32)  NULL,
  position_shown  SMALLINT     NULL,
  created_at      DATETIME(6)  NOT NULL,
  updated_at      DATETIME(6)  NOT NULL,
  UNIQUE KEY uq_user_req_app_src (user_id, request_id, source_app, source_id),
  CONSTRAINT ck_vote_unit CHECK (vote IN (-1, 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def _iter_statements(sql: str) -> Iterator[str]:
    for raw in sql.split(";"):
        stmt = raw.strip()
        if stmt:
            yield stmt


def upgrade() -> None:
    conn = op.get_bind()
    for stmt in _iter_statements(_SQL):
        conn.exec_driver_sql(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS feedback")
    op.execute("DROP TABLE IF EXISTS system_settings")
    op.execute("DROP TABLE IF EXISTS documents")
