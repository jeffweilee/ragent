-- 017_pat.sql — one encrypted Personal Access Token (PAT) per user (T-PAT).
--
-- A PAT is a user-owned, SSO-signed JWT that authorizes ragent (the downstream
-- agent) to act on the user's behalf against upstream services (drive tool,
-- reached through brain). ONE nt ⇄ ONE PAT: `user_id` is UNIQUE so the database
-- — not application code — refuses a second PAT for the same owner, and the
-- authorize/refresh writes are a single `INSERT … ON DUPLICATE KEY UPDATE`
-- overwrite. The DB row always holds the *current* token (every successful
-- refresh rewrites it), never a stale "initial" one.
--
-- `pat_cipher` is the AES-256-GCM envelope (a compact string) — the plaintext
-- PAT is never stored. TEXT (not VARCHAR): a PAT JWT plus its envelope can run a
-- few KB, comfortably inside TEXT's 64 KiB and well past VARCHAR row limits.
--
-- `status` is a 2-value flag (`active` | `invalid`). It flips to `invalid` only
-- when the refresh service returns 401 (authorization gone); a re-authorization
-- or a successful refresh resets it to `active`.
--
-- Surrogate id PK per 00_rule.md Database Practices. No physical FK on `user_id`
-- (relationships are application-level). Point lookups are by `user_id` on the
-- UNIQUE `uq_pat_user`, so no extra index is needed.

CREATE TABLE IF NOT EXISTS pat (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id     VARCHAR(64)  NOT NULL,
  pat_cipher  TEXT         NOT NULL,
  status      VARCHAR(16)  NOT NULL DEFAULT 'active',
  created_at  DATETIME(6)  NOT NULL,
  updated_at  DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_pat_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
