-- 014_chat_attachments.sql (downgrade) — drop chat attachment tables.
-- Artifacts first: no physical FK, but this mirrors the dependency order
-- already used by the old alembic/versions/000_squash.py::downgrade().
DROP TABLE IF EXISTS chat_attachment_artifacts;
DROP TABLE IF EXISTS chat_attachments;
