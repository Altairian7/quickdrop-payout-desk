CREATE TABLE IF NOT EXISTS messages (
  message_id  TEXT PRIMARY KEY,
  rider_id    TEXT NOT NULL,
  text        TEXT NOT NULL,
  received_at TIMESTAMPTZ NOT NULL,
  status      TEXT NOT NULL DEFAULT 'processing',   -- processing | done
  reply       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()    -- doubles as a lease for stale-processing takeover
);

CREATE TABLE IF NOT EXISTS trace_steps (
  id       BIGSERIAL PRIMARY KEY,
  rider_id TEXT NOT NULL,
  at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  type     TEXT NOT NULL,
  name     TEXT NOT NULL,
  input    JSONB,
  output   JSONB
);
CREATE INDEX IF NOT EXISTS trace_rider_idx ON trace_steps (rider_id, id);

CREATE TABLE IF NOT EXISTS conversations (
  rider_id   TEXT PRIMARY KEY,
  state      JSONB NOT NULL DEFAULT '{}',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS disputes (
  id           BIGSERIAL PRIMARY KEY,
  dispute_key  TEXT UNIQUE NOT NULL,
  rider_id     TEXT NOT NULL,
  issue_type   TEXT NOT NULL,
  issue_date   DATE NOT NULL,
  trip_ids     TEXT[] NOT NULL DEFAULT '{}',
  amount       INTEGER NOT NULL,
  status       TEXT NOT NULL,  -- paid | in_progress | awaiting_approval | rejected | escalated
  auto_pay_day DATE,           -- set only when auto-paid
  payout_id    TEXT,
  reason       TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- the DB itself refuses a second auto-pay for the same rider on the same day
CREATE UNIQUE INDEX IF NOT EXISTS one_autopay_per_day
  ON disputes (rider_id, auto_pay_day) WHERE auto_pay_day IS NOT NULL;

CREATE TABLE IF NOT EXISTS ops_items (
  id          BIGSERIAL PRIMARY KEY,
  rider_id    TEXT NOT NULL,
  dispute_id  BIGINT REFERENCES disputes(id),
  type        TEXT NOT NULL,                     -- approval | escalation
  amount      INTEGER,
  reason      TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ
);
