-- 012_documents_status_created_index.sql (downgrade) — drop idx_status_created.
ALTER TABLE documents
  DROP INDEX idx_status_created,
  ALGORITHM=INPLACE, LOCK=NONE;
