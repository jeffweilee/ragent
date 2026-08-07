-- 018_pat_authorization_window.sql — record which authorization window a PAT
-- belongs to (T-PAT.25).
--
-- The init service mints a PAT with an `expireDate` (`PAT_INIT_EXPIRE_DAYS`,
-- 360) and that window is what actually ends a user's authorization — the JWT's
-- own 12 h `exp` is rotated transparently by refresh and never reaches the user.
-- Re-authorizing restarts the window, so it is a property of the *last*
-- authorize, and neither existing timestamp can identify it: `created_at` is
-- written only by the first INSERT, and `updated_at` is overwritten by every
-- 12 h refresh. Without these two columns there is no way to warn a user before
-- their window closes, or to see a cohort of users expiring together.
--
-- Written by authorize ONLY. `rotate` (refresh) deliberately leaves both alone —
-- rotating a token must never appear to extend the authorization behind it.
--
-- `authorization_expires_at` is DATE, not DATETIME: the value we send to init is
-- `YYYY/MM/DD` and the upstream evaluates it in its own timezone, so the real
-- moment of expiry is fuzzy by up to a day. Storing a timestamp would fake a
-- precision we do not have. It is a *prediction* — the upstream stays
-- authoritative and a refresh 401 remains the only thing that invalidates a
-- PAT — so it may be read for display and alerting, never as a gate.
--
-- Both NULLable with NO backfill: rows written before this migration have an
-- unknown window. Deriving one from `created_at + PAT_INIT_EXPIRE_DAYS` would be
-- wrong for anyone who re-authorized, and would err toward "no warning when one
-- is due". NULL honestly means unknown; those users still fall out through the
-- refresh-401 path.

ALTER TABLE pat
  ADD COLUMN authorized_at DATETIME(6) NULL AFTER status,
  ADD COLUMN authorization_expires_at DATE NULL AFTER authorized_at;
