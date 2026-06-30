-- 004_documents_mime_type.sql (downgrade) — drop the mime_type column.
ALTER TABLE documents
  DROP COLUMN mime_type,
  ALGORITHM=INSTANT;
