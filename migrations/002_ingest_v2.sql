-- 002_ingest_v2.sql — Ingest API v2 (spec §3.1 v2 OVERRIDE).
-- Adds discriminator + multi-site + citation URL columns to `documents`.
-- The `chunks` table is retained at this commit; deleted in C6 once the v2
-- pipeline (C4) has stopped writing to it. Each commit must be independently
-- applicable.

ALTER TABLE documents
  ADD COLUMN ingest_type ENUM('inline','file') NOT NULL DEFAULT 'inline' AFTER object_key,
  ADD COLUMN minio_site  VARCHAR(64)  NULL                                AFTER ingest_type,
  ADD COLUMN source_url  VARCHAR(2048) NULL                               AFTER source_workspace,
  ALGORITHM=INSTANT;
