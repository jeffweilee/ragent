-- 002_ingest_v2.sql — Ingest API v2 (spec §3.1 v2 OVERRIDE).
-- Adds discriminator + multi-site + citation URL columns to `documents`.
-- The `chunks` table is retained at this commit; deleted in C6 once the v2
-- pipeline (C4) has stopped writing to it. Each commit must be independently
-- applicable.
--
-- Columns are appended to the end of the table (no AFTER clause): MariaDB
-- 10.6 ALGORITHM=INSTANT only supports appending. `schema.sql` mirrors this
-- ordering so `mysqldump` of `alembic upgrade head` matches `mysqldump` of
-- `schema.sql` byte-for-byte (T0.8b drift test).

ALTER TABLE documents
  ADD COLUMN ingest_type ENUM('inline','file') NOT NULL DEFAULT 'inline',
  ADD COLUMN minio_site  VARCHAR(64)  NULL,
  ADD COLUMN source_url  VARCHAR(2048) NULL,
  ALGORITHM=INSTANT;
