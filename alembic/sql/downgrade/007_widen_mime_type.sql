-- 007_widen_mime_type.sql (downgrade) — narrow mime_type back to VARCHAR(64).
-- Only safe if no persisted value exceeds 64 chars (true for the formats
-- supported before DOCX/PPTX onboarding).
-- VARCHAR narrowing isn't a supported INSTANT change on MariaDB 10.6 (it
-- must validate existing row lengths), unlike the upgrade's widening.
ALTER TABLE documents
  MODIFY COLUMN mime_type VARCHAR(64) NULL,
  ALGORITHM=COPY;
