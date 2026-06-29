-- 014_chat_attachments.sql — chat-attachment metadata + per-AST-variant
-- storage pointers (T-CAT.7/T-CAT.W2, docs/spec/chat_attachments.md §9).
--
-- Squashes 015_chat_attachment_artifacts_content_type.sql (content_type
-- column) and 016_drop_chat_attachment_artifacts_fk.sql (FK removal) into
-- this revision — same rationale as T-CAT.W5's earlier squash: neither
-- init_schema.py nor alembic/versions/000_squash.py ever replays numbered
-- migration files (both read migrations/schema.sql directly), so the
-- 0XX_*.sql files are documentation-only and safe to consolidate.
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
-- envelope was written under (T-CAT.6). `content_type` records the
-- rendered MIME of the artifact's plaintext (`ARTIFACT_CONTENT_TYPE`,
-- currently always 'text/markdown') as a queryable column — never inside
-- the encrypted envelope, since `ASTCipher.decrypt_ast()` never reads
-- envelope metadata back. No physical FOREIGN KEY on attachment_id
-- (docs/00_rule.md "No Physical Foreign Keys" — relationships belong only
-- in application-level ORM models); `uq_attachment_variant`'s leftmost
-- prefix already covers attachment_id lookups.
-- `char_count` is the rendered markdown's character length, computed once at
-- artifact-creation time (ChatAttachmentService.process(), free — the
-- plaintext string is already in memory before encryption). DocumentArtifactResolver
-- uses it to gate `complete` vs `simplified` selection against
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
