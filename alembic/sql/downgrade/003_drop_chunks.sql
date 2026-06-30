-- 003_drop_chunks.sql (downgrade) — recreate `chunks` exactly as defined in 001_initial.sql.
CREATE TABLE IF NOT EXISTS chunks (
  chunk_id    CHAR(26)   NOT NULL,
  document_id CHAR(26)   NOT NULL,
  ord         INT        NOT NULL,
  text        MEDIUMTEXT NOT NULL,
  lang        VARCHAR(8) NOT NULL,
  PRIMARY KEY (chunk_id),
  INDEX idx_document (document_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
