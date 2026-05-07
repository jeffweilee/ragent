-- 004_documents_mime_type.sql — store the request mime alongside the document.
--
-- Persisting the mime keeps Prometheus metric labels honest: the splitter
-- already routes per `meta["mime_type"]`, but the value was previously known
-- only inside the running pipeline. Recording it on `documents` lets the
-- DocumentStatsCollector group counts by mime without a second source of
-- truth.
--
-- Appended at end of table: MariaDB 10.6 ALGORITHM=INSTANT only supports
-- appending. NULL allowed so the migration is online and pre-existing rows
-- need no backfill — emission code treats NULL as `text/plain` (the v2 router
-- default for unknown mime is to fail, so existing READY rows that were
-- ingested before this column existed are necessarily one of the supported
-- mimes, treating them as `text/plain` is a safe metric bucket).

ALTER TABLE documents
  ADD COLUMN mime_type VARCHAR(64) NULL,
  ALGORITHM=INSTANT;
