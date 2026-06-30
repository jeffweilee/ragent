-- 012_documents_status_created_index.sql
-- Adds idx_status_created to cover count_by_statuses / list_by_statuses
-- queries that filter on (status, created_after) without source_app.
-- Without this index those queries fall back to idx_status_updated and
-- apply the created_at predicate as a residual filter on matched rows.
-- ALGORITHM=INPLACE, LOCK=NONE — no table rebuild on MariaDB 10.6.
ALTER TABLE documents
  ADD INDEX idx_status_created (status, created_at),
  ALGORITHM=INPLACE, LOCK=NONE;
