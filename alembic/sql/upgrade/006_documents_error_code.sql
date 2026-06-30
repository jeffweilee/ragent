-- 006_documents_error_code.sql — persist failure diagnostics on documents.
--
-- Async ingest failures previously stored only `status='FAILED'` on the row.
-- The worker log carried the actual `error_code` (e.g. EMBEDDER_ERROR,
-- PIPELINE_TIMEOUT_AGGREGATE) but `GET /ingest/{document_id}` had no way to
-- surface it, so a downstream API polling for completion received only a
-- generic FAILED with no diagnostic for branching its retry policy.
--
-- 00_rule.md §API Error Honesty: "async task failures MUST persist
-- error_code + error_reason on the document row and the corresponding
-- GET /<resource>/{id} endpoint MUST return both fields alongside
-- status='FAILED'."
--
-- Appended at end of table: MariaDB 10.6 ALGORITHM=INSTANT only supports
-- appending. NULL allowed so the migration is online and pre-existing rows
-- need no backfill — historical FAILED rows just expose NULL for both.
-- error_reason is bounded to 255 chars (sufficient for the truncated form of
-- the f"{ExceptionName} {msg}" output) -- full tracebacks live in worker
-- logs only, never on the row.

ALTER TABLE documents
  ADD COLUMN error_code VARCHAR(64) NULL,
  ADD COLUMN error_reason VARCHAR(255) NULL,
  ALGORITHM=INSTANT;
