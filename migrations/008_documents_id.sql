-- 008_documents_id.sql — add surrogate auto-increment id column to documents.
--
-- Adds `id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT` as the new PRIMARY KEY,
-- positioned FIRST. `document_id` retains a UNIQUE constraint so all existing
-- lookups by ULID continue to work unchanged.
--
-- This migration requires ALGORITHM=COPY: swapping the clustered PK forces a
-- full table rebuild. INSTANT/INPLACE cannot reorder the clustered index.
-- Expect a brief table lock proportional to table size.

ALTER TABLE documents
  DROP PRIMARY KEY,
  ADD COLUMN id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT FIRST,
  ADD PRIMARY KEY (id),
  ADD UNIQUE KEY uq_document_id (document_id),
  ALGORITHM=COPY;
