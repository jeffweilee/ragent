-- schema.sql — consolidated snapshot reflecting alembic head (spec B3).
-- Updated in lockstep with every NNN_*.sql migration file.
-- Apply directly: mysql -u user -p ragent < schema.sql
-- Or via Alembic:  alembic upgrade head  (produces identical schema)

CREATE TABLE IF NOT EXISTS documents (
  document_id      CHAR(26)     NOT NULL,
  create_user      VARCHAR(64)  NOT NULL,
  source_id        VARCHAR(128) NOT NULL,
  source_app       VARCHAR(64)  NOT NULL,
  source_title     VARCHAR(256) NOT NULL,
  source_workspace VARCHAR(64)  NULL,
  source_url       VARCHAR(2048) NULL,
  object_key       VARCHAR(256) NOT NULL,
  ingest_type      ENUM('inline','file') NOT NULL DEFAULT 'inline',
  minio_site       VARCHAR(64)  NULL,
  status           ENUM('UPLOADED','PENDING','READY','FAILED','DELETING') NOT NULL,
  attempt          INT          NOT NULL DEFAULT 0,
  created_at       DATETIME(6)  NOT NULL,
  updated_at       DATETIME(6)  NOT NULL,
  PRIMARY KEY (document_id),
  INDEX idx_status_updated (status, updated_at),
  INDEX idx_source_app_id_status_created (source_app, source_id, status, created_at),
  INDEX idx_create_user_document (create_user, document_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS chunks (
  chunk_id    CHAR(26)   NOT NULL,
  document_id CHAR(26)   NOT NULL,
  ord         INT        NOT NULL,
  text        MEDIUMTEXT NOT NULL,
  lang        VARCHAR(8) NOT NULL,
  PRIMARY KEY (chunk_id),
  INDEX idx_document (document_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
