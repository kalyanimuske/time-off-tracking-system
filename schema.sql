-- Production schema (PostgreSQL) for the time off system.
-- The Python code uses an in-memory store with the same shape (app/storage/memory.py).
--
-- Conventions
--   * All balance amounts are integer MINUTES.
--   * Money-like data is append-only: balance_transactions and policy_versions
--     are never updated or deleted.
--   * employees / companies live in the existing Employee and Company services,
--     so they are referenced by id only (no foreign keys across services).

CREATE TYPE time_off_type      AS ENUM ('vacation', 'sick', 'parental', 'bereavement', 'wfh', 'other');
CREATE TYPE accrual_method     AS ENUM ('unlimited', 'time_based', 'hours_worked');
CREATE TYPE accrual_frequency  AS ENUM ('monthly', 'yearly');
CREATE TYPE accrual_timing     AS ENUM ('start_of_period', 'end_of_period');
CREATE TYPE rate_unit          AS ENUM ('days', 'hours');
CREATE TYPE proration_rule     AS ENUM ('prorate', 'full', 'next_period');
CREATE TYPE request_status     AS ENUM ('pending', 'approved', 'rejected', 'cancelled');
CREATE TYPE transaction_type   AS ENUM ('accrual', 'hold', 'hold_release', 'usage',
                                        'usage_reversal', 'expiration', 'adjustment');

-- --------------------------------------------------------------------------
-- Policies
-- --------------------------------------------------------------------------

CREATE TABLE policies (
    id              TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL,
    name            TEXT NOT NULL,
    time_off_type   time_off_type NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    is_paid         BOOLEAN NOT NULL DEFAULT TRUE,
    archived_at     TIMESTAMPTZ,
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX policies_company_idx ON policies (company_id);

-- Immutable. Editing a policy inserts a new row with a later effective_from.
CREATE TABLE policy_versions (
    id                        TEXT PRIMARY KEY,
    policy_id                 TEXT NOT NULL REFERENCES policies (id),
    version_number            INT  NOT NULL,
    effective_from            DATE NOT NULL,
    accrual_method            accrual_method NOT NULL,
    frequency                 accrual_frequency,
    accrue_at                 accrual_timing NOT NULL DEFAULT 'start_of_period',
    rate_amount               NUMERIC(10, 4) NOT NULL DEFAULT 0,
    rate_unit                 rate_unit NOT NULL DEFAULT 'days',
    hours_worked_per_accrual  NUMERIC(10, 4),
    max_balance_hours         NUMERIC(10, 2),
    carryover_cap_hours       NUMERIC(10, 2),          -- NULL = unlimited carryover
    allow_negative            BOOLEAN NOT NULL DEFAULT FALSE,
    max_negative_hours        NUMERIC(10, 2) NOT NULL DEFAULT 0,
    proration_rule            proration_rule NOT NULL DEFAULT 'prorate',
    waiting_period_days       INT NOT NULL DEFAULT 0,
    tenure_tiers              JSONB NOT NULL DEFAULT '[]',  -- [{"min_years": 5, "rate_amount": 25}]
    created_by                TEXT NOT NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (policy_id, version_number),
    UNIQUE (policy_id, effective_from),
    CHECK (accrual_method <> 'time_based'   OR frequency IS NOT NULL),
    CHECK (accrual_method <> 'hours_worked' OR hours_worked_per_accrual > 0),
    CHECK (NOT allow_negative OR max_negative_hours > 0)
);

-- Which employees are on which policy, and when. Supports any grouping
-- (full-time vs part-time, employee vs contractor, per state, ...).
CREATE TABLE policy_assignments (
    id           TEXT PRIMARY KEY,
    policy_id    TEXT NOT NULL REFERENCES policies (id),
    employee_id  TEXT NOT NULL,
    start_date   DATE NOT NULL,
    end_date     DATE,                                  -- inclusive, NULL = open-ended
    created_by   TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (end_date IS NULL OR end_date >= start_date)
);
CREATE INDEX policy_assignments_employee_idx ON policy_assignments (employee_id);
CREATE INDEX policy_assignments_policy_idx   ON policy_assignments (policy_id);
-- "One policy per time off type at a time" is enforced in the service layer
-- because the type lives on `policies`. An exclusion constraint on a
-- denormalized time_off_type column would enforce it in the DB as well.

-- --------------------------------------------------------------------------
-- Holidays
-- --------------------------------------------------------------------------

CREATE TABLE company_holidays (
    id          TEXT PRIMARY KEY,
    company_id  TEXT NOT NULL,
    day         DATE NOT NULL,
    name        TEXT NOT NULL,
    location    TEXT,                                    -- NULL = whole company
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX company_holidays_unique_idx
    ON company_holidays (company_id, day, COALESCE(location, ''));

-- --------------------------------------------------------------------------
-- Requests
-- --------------------------------------------------------------------------

CREATE TABLE time_off_requests (
    id             TEXT PRIMARY KEY,
    employee_id    TEXT NOT NULL,
    policy_id      TEXT NOT NULL REFERENCES policies (id),
    reason         TEXT NOT NULL,
    status         request_status NOT NULL DEFAULT 'pending',
    start_date     DATE NOT NULL,                        -- denormalized for fast range queries
    end_date       DATE NOT NULL,
    total_minutes  INT  NOT NULL,
    decided_by     TEXT,
    decided_at     TIMESTAMPTZ,
    decision_note  TEXT,                              -- optional note on approve / reject
    cancelled_by   TEXT,
    cancelled_at   TIMESTAMPTZ,
    cancel_reason  TEXT,                              -- required when an admin revokes
    created_by     TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX time_off_requests_employee_idx ON time_off_requests (employee_id, start_date);
CREATE INDEX time_off_requests_status_idx   ON time_off_requests (status);

-- One row per working day in the request (weekends / holidays are not stored).
CREATE TABLE time_off_request_days (
    request_id  TEXT NOT NULL REFERENCES time_off_requests (id),
    day         DATE NOT NULL,
    minutes     INT  NOT NULL CHECK (minutes >= 0),
    PRIMARY KEY (request_id, day)
);

-- Every status change: who, when, from what, to what, why.
CREATE TABLE time_off_request_events (
    id           TEXT PRIMARY KEY,
    request_id   TEXT NOT NULL REFERENCES time_off_requests (id),
    from_status  request_status,
    to_status    request_status NOT NULL,
    actor_id     TEXT NOT NULL,
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX time_off_request_events_request_idx ON time_off_request_events (request_id);

-- --------------------------------------------------------------------------
-- Balance ledger (append-only). Balance = SUM(amount_minutes).
-- --------------------------------------------------------------------------

CREATE TABLE balance_transactions (
    id                 TEXT PRIMARY KEY,
    employee_id        TEXT NOT NULL,
    policy_id          TEXT NOT NULL REFERENCES policies (id),
    policy_version_id  TEXT REFERENCES policy_versions (id),
    amount_minutes     INT  NOT NULL,                    -- positive = credit, negative = debit
    type               transaction_type NOT NULL,
    effective_date     DATE NOT NULL,
    idempotency_key    TEXT NOT NULL UNIQUE,             -- makes jobs and event handlers safe to retry
    source_type        TEXT,                             -- 'time_off_request', 'payroll_run', 'accrual_period', 'year_end', 'manual'
    source_id          TEXT,
    note               TEXT NOT NULL DEFAULT '',
    metadata           JSONB NOT NULL DEFAULT '{}',
    created_by         TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX balance_transactions_balance_idx ON balance_transactions (employee_id, policy_id, effective_date);
CREATE INDEX balance_transactions_source_idx  ON balance_transactions (source_type, source_id);

-- Optional read cache, updated in the same DB transaction as each ledger insert.
-- The ledger stays the source of truth; this can always be rebuilt from it.
CREATE TABLE balance_snapshots (
    employee_id       TEXT NOT NULL,
    policy_id         TEXT NOT NULL REFERENCES policies (id),
    settled_minutes   INT NOT NULL,
    pending_minutes   INT NOT NULL,
    last_txn_id       TEXT NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (employee_id, policy_id)
);

-- --------------------------------------------------------------------------
-- Generic audit log (policy changes, assignments, holidays, request changes)
-- --------------------------------------------------------------------------

CREATE TABLE audit_log (
    id           TEXT PRIMARY KEY,
    entity_type  TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    action       TEXT NOT NULL,
    actor_id     TEXT NOT NULL,
    before       JSONB,
    after        JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_entity_idx ON audit_log (entity_type, entity_id, created_at);
