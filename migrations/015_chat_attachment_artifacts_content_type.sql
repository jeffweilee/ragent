-- 015_chat_attachment_artifacts_content_type.sql — persist the AST artifact's
-- rendered content_type as a real, queryable DB column instead of inside the
-- encrypted envelope (docs/spec/chat_attachments.md §10). `ASTCipher.decrypt_ast()`
-- never reads envelope metadata back, so a previous attempt to store content_type
-- there was write-only; the DB column is queryable without decrypting and avoids
-- duplicating state.
--
-- ALGORITHM=INSTANT: appending a column at the end of the table with a
-- literal DEFAULT (NOT NULL or nullable) is instant on MariaDB 10.6 (no
-- full-table rewrite) — same "append, don't reorder" shape as
-- 006_documents_error_code.sql's ADD COLUMN, which used nullable defaults
-- instead of a literal value.

ALTER TABLE chat_attachment_artifacts
  ADD COLUMN content_type VARCHAR(64) NOT NULL DEFAULT 'text/markdown',
  ALGORITHM=INSTANT;
