-- 011_ingest_type_upload.sql (downgrade) — drop the 'upload' discriminator.
-- Only safe if no row currently has ingest_type='upload' — same manual
-- backfill caveat as the upgrade's own comment; an operator must reconcile
-- those rows (e.g. to 'inline') before downgrading past this revision.
-- Removing an ENUM value (vs. the upgrade's append-only widening) isn't a
-- supported INSTANT change on MariaDB 10.6; it requires a table rebuild.
ALTER TABLE documents
  MODIFY COLUMN ingest_type ENUM('inline','file') NOT NULL DEFAULT 'inline',
  ALGORITHM=COPY;
