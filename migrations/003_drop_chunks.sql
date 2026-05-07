-- 003_drop_chunks.sql — v2 cleanup: chunks live only in ES `chunks_v1`.
-- The MariaDB `chunks` table is no longer written to by the v2 pipeline
-- (C4: DocumentWriter targets ElasticsearchDocumentStore exclusively).

DROP TABLE IF EXISTS chunks;
