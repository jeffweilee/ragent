-- 018_pat_authorization_window.sql (downgrade) — drop the authorization-window
-- columns. The PAT itself is untouched: these two carry only the *prediction* of
-- when the window closes, never the credential, so dropping them costs the
-- warning/alerting surface and nothing else.

ALTER TABLE pat
  DROP COLUMN authorization_expires_at,
  DROP COLUMN authorized_at;
