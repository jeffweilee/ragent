-- 002_ingest_v2.sql (downgrade) — drop the v2 discriminator/site/url columns.
ALTER TABLE documents
  DROP COLUMN ingest_type,
  DROP COLUMN minio_site,
  DROP COLUMN source_url,
  ALGORITHM=INSTANT;
