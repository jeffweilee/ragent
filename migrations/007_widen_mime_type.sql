-- 007_widen_mime_type.sql — widen mime_type column from VARCHAR(64) to VARCHAR(128).
--
-- The original VARCHAR(64) predates DOCX/PPTX support. The longest registered
-- MIME type for those formats is 80 chars:
--   application/vnd.openxmlformats-officedocument.presentationml.presentation
-- VARCHAR(128) covers all current types with headroom for future additions.
--
-- ALGORITHM=INSTANT: MariaDB 10.6 supports instant VARCHAR widening when the
-- new length fits in the same length-byte class (both 64 and 128 require 1
-- length byte for utf8mb4, so this is instant and causes zero table locking).

ALTER TABLE documents
  MODIFY COLUMN mime_type VARCHAR(128) NULL,
  ALGORITHM=INSTANT;
