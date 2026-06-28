-- 016_drop_chat_attachment_artifacts_fk.sql — drop the physical FOREIGN KEY
-- on chat_attachment_artifacts.attachment_id, added in 014_chat_attachments.sql
-- in violation of docs/00_rule.md's "No Physical Foreign Keys" rule (FK
-- relationships belong only in application-level ORM models, never as a DB
-- constraint — simplifies migrations and improves bulk write performance).
--
-- The UNIQUE KEY uq_attachment_variant (attachment_id, variant) already
-- covers attachment_id as a leftmost prefix, so no replacement index is
-- needed for lookup/join performance.
--
-- DROP FOREIGN KEY is an InnoDB metadata-only change (no table rebuild),
-- so no ALGORITHM clause is required.

ALTER TABLE chat_attachment_artifacts
  DROP FOREIGN KEY fk_artifact_attachment;
