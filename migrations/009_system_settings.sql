-- 009_system_settings.sql — generic key/JSON settings table (B50).
--
-- Single-table backing store for runtime-mutable system settings that the
-- App reads via TTL-cached ActiveModelRegistry. Future settings (rate-limit
-- overrides, feature flags) live alongside in the same table without
-- further migrations.
--
-- Surrogate id PK per 00_rule.md Database Practices. Business key is
-- setting_key and is UNIQUE-constrained so application code cannot
-- create duplicates by accident.
--
-- Seed rows are NOT in this file. They are inserted by the alembic
-- upgrader (versions/009_system_settings.py) using parameterized SQL —
-- file-level `split(";")` is fragile when seed JSON ever needs to
-- contain a semicolon (URL query strings, model args, etc.).

CREATE TABLE IF NOT EXISTS system_settings (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  setting_key   VARCHAR(64) NOT NULL,
  setting_value JSON NOT NULL,
  updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_setting_key (setting_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
