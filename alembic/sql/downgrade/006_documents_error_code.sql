-- 006_documents_error_code.sql (downgrade) — drop the failure-diagnostic columns.
ALTER TABLE documents
  DROP COLUMN error_code,
  DROP COLUMN error_reason,
  ALGORITHM=INSTANT;
