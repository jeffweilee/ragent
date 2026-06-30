-- 008_documents_id.sql (downgrade) — restore document_id as the clustered PK.
-- Requires ALGORITHM=COPY, same as the upgrade: swapping the clustered PK
-- forces a full table rebuild.
ALTER TABLE documents
  DROP PRIMARY KEY,
  DROP COLUMN id,
  DROP KEY uq_document_id,
  ADD PRIMARY KEY (document_id),
  ALGORITHM=COPY;
