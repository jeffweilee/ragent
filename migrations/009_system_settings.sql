-- 009_system_settings.sql — generic key/JSON settings table (B50).
--
-- Single-table backing store for runtime-mutable system settings that the
-- App reads via TTL-cached ActiveModelRegistry. Four embedding-lifecycle
-- rows are seeded. Future settings (rate-limit overrides, feature flags)
-- live alongside in the same table without further migrations.
--
-- Surrogate id PK per 00_rule.md Database Practices. Business key is
-- setting_key and is UNIQUE-constrained so application code cannot
-- create duplicates by accident.

CREATE TABLE IF NOT EXISTS system_settings (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  setting_key   VARCHAR(64) NOT NULL,
  setting_value JSON NOT NULL,
  updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_setting_key (setting_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Seed the four embedding-lifecycle rows. INSERT IGNORE keeps re-applying
-- the migration on an upgraded cluster idempotent (matches the boot
-- auto-init contract — never mutate existing data).
INSERT IGNORE INTO system_settings (setting_key, setting_value) VALUES
  ('embedding.stable',    '{"name":"bge-m3","dim":1024,"api_url":"","model_arg":"bge-m3","field":"embedding_bgem3_1024"}'),
  ('embedding.candidate', 'null'),
  ('embedding.read',      '"stable"'),
  ('embedding.retired',   '[]');
