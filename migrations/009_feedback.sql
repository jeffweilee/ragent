-- 009_feedback.sql — feedback events for the per-source ranking signal (T-FB.3, B50/B51).
--
-- Append-only event log of user feedback on chat sources. MariaDB stores
-- meta only — never the chat query text, never the answer text. The ES
-- `feedback_v1` index (T-FB.5) holds the query embedding + reason for the
-- kNN-based feedback retriever; this table is the source of truth and the
-- replay source for ES recovery (B51, B53 item 7).
--
-- Idempotency: `(user_id, request_id, source_id)` is unique — repeated
-- POST /feedback/v1 with the same triple is an UPSERT (T-FB.4 last-write-wins).
--
-- IDs follow §5.3: `feedback_id` is a CHAR(26) UUIDv7→Crockford Base32 via
-- `new_id()`; `request_id` is the same shape emitted by /chat.
--
-- No secondary indexes in P1 — per B53 review, only the uniqueness index
-- is needed until a concrete query path requires aggregation (analytics /
-- replay job paths come in P2 and will introduce their own indexes then).

CREATE TABLE IF NOT EXISTS feedback (
  feedback_id     CHAR(26)     PRIMARY KEY,
  request_id      CHAR(26)     NOT NULL,
  user_id         VARCHAR(64)  NOT NULL,
  source_id       VARCHAR(128) NOT NULL,
  vote            TINYINT      NOT NULL,
  reason          VARCHAR(32)  NULL,
  position_shown  SMALLINT     NULL,
  created_at      DATETIME(6)  NOT NULL,
  updated_at      DATETIME(6)  NOT NULL,
  UNIQUE KEY uq_user_req_src (user_id, request_id, source_id),
  CONSTRAINT ck_vote_unit CHECK (vote IN (-1, 1))
);
