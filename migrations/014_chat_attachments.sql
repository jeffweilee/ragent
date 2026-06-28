-- 014_chat_attachments.sql — chat-attachment metadata + per-AST-variant
-- storage pointers (T-CAT.7/T-CAT.W2, docs/spec/chat_attachments.md §9).
--
-- No `introduced_run_id` column — the `<hidden><attachments>` block already
-- binds an attachment to the turn it was attached on; no DB-side binding
-- is needed (per spec §7).
--
-- `chat_attachments.status` follows the same insert-then-update lifecycle as
-- `documents.status`: a row is written 'UPLOADED' as soon as the raw bytes
-- are stored, flipped to 'PROCESSING' when the worker claims it, then to
-- 'READY' or 'FAILED' once the AST-build + encrypt + artifact-store steps
-- finish (service layer, T-CAT.11/T-CAT.W2). `error_code`/`error_reason`
-- carry failure diagnostics for the FAILED terminal state — same rationale
-- as `documents.error_code`/`error_reason` (006_documents_error_code.sql);
-- full tracebacks live in worker logs only.
--
-- No thread-ownership check on reads (spec §7) — isolation is `create_user`
-- plus the query predicate, not an authorization check; same trust model
-- `documents` already uses.
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

-- One artifact row per AST variant ('complete'/'simplified', spec §4) per
-- attachment. `storage_key` is the DocumentStore object key the encrypted
-- envelope was written under (T-CAT.6).
CREATE TABLE IF NOT EXISTS chat_attachment_artifacts (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  attachment_id CHAR(26)     NOT NULL,
  variant       ENUM('complete','simplified') NOT NULL,
  storage_key   VARCHAR(256) NOT NULL,
  created_at    DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_attachment_variant (attachment_id, variant),
  CONSTRAINT fk_artifact_attachment FOREIGN KEY (attachment_id)
    REFERENCES chat_attachments (attachment_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
