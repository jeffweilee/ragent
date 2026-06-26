-- 015_chat_artifact_ast_type_to_variant.sql — rename ast_type → variant for
-- semantic clarity (T-CAT.W4, docs/spec/chat_attachments.md §2.1).
--
-- The artifact model uses two axes: variant (complete/simplified — which version
-- for retrieval), and future content_type (storage format hint, deferred).
-- Renaming the column clarifies intent and aligns with docs terminology.
--
-- Appended, non-blocking column rename using CHANGE (ALGORITHM=INSTANT on MariaDB 10.6+).
ALTER TABLE chat_attachment_artifacts
  CHANGE COLUMN ast_type variant ENUM('complete','simplified') NOT NULL,
  ALGORITHM=INSTANT;
