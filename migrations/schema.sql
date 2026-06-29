-- schema.sql — consolidated snapshot reflecting alembic head (spec B3).
-- Latest migration folded in: 014_chat_attachments.sql
-- Updated in lockstep with every NNN_*.sql migration file.
-- Apply directly: mysql -u user -p ragent < schema.sql
-- Or via Alembic:  alembic upgrade head  (produces identical schema)

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
  -- v2 columns (002_ingest_v2.sql). Appended at end so ALGORITHM=INSTANT
  -- in alembic ALTER produces an identical column ordering (drift test).
  -- 011_ingest_type_upload.sql widened the enum to add 'upload' for the
  -- multipart POST /ingest/v1/upload path (distinct cleanup contract: blob
  -- survives READY and is reclaimed only by the DELETE API).
  ingest_type      ENUM('inline','file','upload') NOT NULL DEFAULT 'inline',
  minio_site       VARCHAR(64)  NULL,
  source_url       VARCHAR(2048) NULL,
  -- 004_documents_mime_type.sql: appended NULL to keep ALGORITHM=INSTANT online.
  -- 007_widen_mime_type.sql: widened to VARCHAR(128) for DOCX/PPTX MIME strings (up to 80 chars).
  mime_type        VARCHAR(128) NULL,
  -- 006_documents_error_code.sql: failure diagnostics for async task failures.
  error_code       VARCHAR(64)  NULL,
  error_reason     VARCHAR(255) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_document_id (document_id),
  INDEX idx_status_updated (status, updated_at),
  INDEX idx_status_created (status, created_at),
  INDEX idx_source_app_id_status_created (source_app, source_id, status, created_at),
  INDEX idx_create_user_document (create_user, document_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- v1 `chunks` table dropped in 003_drop_chunks.sql.
-- v2 stores chunks exclusively in ES (`chunks_v1` index).

-- 009_system_settings.sql: generic key/JSON settings store (B50).
-- Backs the embedding-model lifecycle (`embedding.stable`/`candidate`/`read`/`retired`)
-- and any future runtime-mutable settings without per-row schema migrations.
CREATE TABLE IF NOT EXISTS system_settings (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  setting_key   VARCHAR(64) NOT NULL,
  setting_value JSON NOT NULL,
  updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_setting_key (setting_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Seed the four embedding-lifecycle rows so a fresh boot (init_schema in
-- dev / e2e / first-time prod) has a stable model immediately readable by
-- ActiveModelRegistry.refresh(). INSERT IGNORE keeps re-applying idempotent.
-- JSON_OBJECT / JSON_QUOTE / JSON_ARRAY are used instead of inline string
-- literals so the SQL contains no `:` characters (SQLAlchemy text() would
-- otherwise treat `:bge` etc. as bind parameters and bind-validate them).
INSERT IGNORE INTO system_settings (setting_key, setting_value) VALUES
  ('embedding.stable',    JSON_OBJECT('name','bge-m3','dim',1024,'api_url','','model_arg','bge-m3','field','embedding_bgem3_1024')),
  ('embedding.candidate', 'null'),
  ('embedding.read',      JSON_QUOTE('stable')),
  ('embedding.retired',   JSON_ARRAY());

-- 010_feedback.sql: append-only feedback events (T-FB.3, B54/B55).
-- MariaDB stores meta only. ES `feedback_v1` (§5.4) holds the query
-- embedding and reason text. Idempotency key is the UNIQUE quadruple.
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

-- 014_chat_attachments.sql: chat-attachment metadata + per-AST-variant
-- storage pointers, including PROCESSING (async worker hand-off) and
-- error_code/error_reason failure diagnostics (T-CAT.7/T-CAT.W2). No
-- `introduced_run_id` — the `<hidden><attachments>` block already binds
-- the attachment to its turn. Squashes the former 015 (content_type
-- column) and 016 (FK removal) revisions into this one file.
CREATE TABLE IF NOT EXISTS chat_attachments (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  attachment_id CHAR(26)     NOT NULL,
  thread_id     VARCHAR(64)  NOT NULL,
  create_user   VARCHAR(64)  NOT NULL,
  filename      VARCHAR(256) NOT NULL,
  mime_type     VARCHAR(128) NOT NULL,
  size_bytes    BIGINT UNSIGNED NOT NULL,
  status        ENUM('UPLOADED','PROCESSING','READY','FAILED') NOT NULL DEFAULT 'UPLOADED',
  created_at    DATETIME(6)  NOT NULL,
  updated_at    DATETIME(6)  NOT NULL,
  error_code    VARCHAR(64)  NULL,
  error_reason  VARCHAR(255) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_attachment_id (attachment_id),
  INDEX idx_thread_created (thread_id, created_at),
  INDEX idx_create_user_attachment (create_user, attachment_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- No physical FK on attachment_id (docs/00_rule.md "No Physical Foreign
-- Keys" — relationships belong only in application-level ORM models).
-- uq_attachment_variant's leftmost prefix already covers attachment_id
-- lookups. content_type folded in from the former 015 revision. char_count
-- (rendered markdown length, computed at creation time) gates
-- complete-vs-simplified selection in DocumentArtifactResolver against
-- ATTACHMENT_ARTIFACT_MAX_CHARS without decrypting the artifact first.
CREATE TABLE IF NOT EXISTS chat_attachment_artifacts (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  attachment_id CHAR(26)     NOT NULL,
  variant       ENUM('complete','simplified') NOT NULL,
  storage_key   VARCHAR(256) NOT NULL,
  content_type  VARCHAR(64)  NOT NULL DEFAULT 'text/markdown',
  char_count    INT UNSIGNED NOT NULL DEFAULT 0,
  created_at    DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_attachment_variant (attachment_id, variant)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 013_skills.sql: per-user reusable instruction/prompt presets ("skills").
-- Every skill is private to its owner, every query filters by user_id.
-- Surrogate id PK, skill_id is the CHAR(26) business key (UNIQUE).
-- (user_id, name) UNIQUE so the DB refuses duplicate names per owner.
-- instructions is MEDIUMTEXT (not TEXT): 16,384 chars * 4 bytes/utf8mb4 char
-- = 65,536 B exceeds TEXT's 65,535 B limit. (user_id, created_at, id) backs the
-- newest-first list without a filesort, point lookups use uq_skill_id.
CREATE TABLE IF NOT EXISTS skills (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  skill_id     CHAR(26)      NOT NULL,
  user_id      VARCHAR(64)   NOT NULL,
  name         VARCHAR(128)  NOT NULL,
  description  VARCHAR(512)  NOT NULL DEFAULT '',
  instructions MEDIUMTEXT    NOT NULL,
  enabled      BOOLEAN       NOT NULL DEFAULT TRUE,
  created_at   DATETIME(6)   NOT NULL,
  updated_at   DATETIME(6)   NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_skill_id (skill_id),
  UNIQUE KEY uq_user_name (user_id, name),
  KEY idx_user_created (user_id, created_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
