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
  source_meta      VARCHAR(1024) NULL,
  object_key       VARCHAR(256) NOT NULL,
  status           ENUM('UPLOADED','PENDING','READY','FAILED','DELETING') NOT NULL,
  attempt          INT          NOT NULL DEFAULT 0,
  created_at       DATETIME(6)  NOT NULL,
  updated_at       DATETIME(6)  NOT NULL,
  -- v2 columns (002_ingest_v2.sql). Appended at end so ALGORITHM=INSTANT
  -- in alembic ALTER produces an identical column ordering (drift test).
  ingest_type      ENUM('inline','file') NOT NULL DEFAULT 'inline',
  minio_site       VARCHAR(64)  NULL,
  source_url       VARCHAR(2048) NULL,
  -- 004_documents_mime_type.sql: appended NULL to keep ALGORITHM=INSTANT online.
  -- 007_widen_mime_type.sql: widened to VARCHAR(128) for DOCX/PPTX MIME strings (up to 80 chars).
  mime_type        VARCHAR(128) NULL,
  -- 006_documents_error_code.sql: failure diagnostics for async task failures.
  error_code       VARCHAR(64)  NULL,
  error_reason     VARCHAR(255) NULL,
  PRIMARY KEY (document_id),
  INDEX idx_status_updated (status, updated_at),
  INDEX idx_source_app_id_status_created (source_app, source_id, status, created_at),
  INDEX idx_create_user_document (create_user, document_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- v1 `chunks` table dropped in 003_drop_chunks.sql.
-- v2 stores chunks exclusively in ES (`chunks_v1` index).
