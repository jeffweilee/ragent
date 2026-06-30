-- 005_rename_source_workspace_to_source_meta.sql (downgrade) — rename back and narrow.
-- Crosses the same 1-byte/2-byte VARCHAR length-prefix boundary as the
-- upgrade, so this also requires ALGORITHM=COPY, LOCK=SHARED.
ALTER TABLE documents
  CHANGE COLUMN source_meta source_workspace VARCHAR(64) NULL,
  ALGORITHM=COPY, LOCK=SHARED;
