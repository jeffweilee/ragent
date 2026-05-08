-- 004_rename_source_workspace_to_source_meta.sql
-- B35 (2026-05-07): rename column source_workspace -> source_meta and widen
-- VARCHAR(64) -> VARCHAR(1024). Field becomes free-format metadata.
--
-- Width crosses the 1-byte / 2-byte VARCHAR length-prefix boundary, so
-- MariaDB cannot use ALGORITHM=INSTANT and falls back to COPY: the table
-- is rebuilt and a SHARED metadata lock is held for the duration. With
-- LOCK=SHARED reads continue but writes are blocked. Schedule during a
-- low-write window on prod; on small `documents` tables the rebuild
-- completes in seconds. ALGORITHM/LOCK are stated explicitly so the
-- planner can't silently pick a different combination.
--
-- ES note: existing `chunks_v1` indexes keep the old field name on
-- upgrade — they need a reindex (or alias swap) before the renamed
-- field can serve filters. Fresh installs pick up the new mapping from
-- resources/es/chunks_v1.json automatically via boot auto-init.

ALTER TABLE documents
  CHANGE COLUMN source_workspace source_meta VARCHAR(1024) NULL,
  ALGORITHM=COPY, LOCK=SHARED;
