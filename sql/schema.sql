-- Schema for the coverage decision service.
--
-- Ground truth lives in its own schema and the service never reads it. The
-- service is allowed `coverage`; the answer key is in `truth`. A decision
-- service that can see the labels it is scored against is worth nothing, and
-- the cheapest way to guarantee it cannot is to make that read a
-- schema-qualified one that stands out in any diff.

DROP SCHEMA IF EXISTS coverage CASCADE;
DROP SCHEMA IF EXISTS truth CASCADE;

CREATE SCHEMA coverage;
CREATE SCHEMA truth;


-- ---------------------------------------------------------------------------
-- What the service reads
-- ---------------------------------------------------------------------------

CREATE TABLE coverage.member (
    member_id       integer PRIMARY KEY,
    plan_id         text    NOT NULL
);

CREATE TABLE coverage.merchant (
    merchant_id     integer PRIMARY KEY,
    -- The provider behind the card terminal. A merchant that is not a health
    -- provider at all is the easy case and is deliberately present: a
    -- decision service that only ever sees providers has never met the
    -- traffic it will actually get.
    is_provider     boolean NOT NULL
);

-- Whether a plan covers a merchant, and since when. This table is the reason
-- a cache can be wrong: rows are superseded during a run, and a cached answer
-- keeps serving the row it was built from.
--
-- Effective-dated rather than mutated in place, so that "what was true at the
-- moment of the decision" is answerable after the fact. Overwriting the row
-- would make every staleness figure in experiment 3 unmeasurable.
CREATE TABLE coverage.network_status (
    plan_id         text    NOT NULL,
    merchant_id     integer NOT NULL REFERENCES coverage.merchant,
    covered         boolean NOT NULL,
    effective_from  bigint  NOT NULL,   -- milliseconds since the run started
    PRIMARY KEY (plan_id, merchant_id, effective_from)
);

CREATE INDEX ON coverage.network_status (plan_id, merchant_id, effective_from DESC);


-- ---------------------------------------------------------------------------
-- What the service writes
-- ---------------------------------------------------------------------------

-- One row per decision the service actually recorded.
--
-- The idempotency guard is a column constraint, not an application rule, and
-- experiment 4 exists to show the difference. `guard` records which variant
-- produced the row so the same table can hold all three runs.
CREATE TABLE coverage.decision (
    decision_id     bigserial PRIMARY KEY,
    run_id          text      NOT NULL,
    guard           text      NOT NULL,
    event_id        text      NOT NULL,   -- the processor's idempotency key
    member_id       integer   NOT NULL,
    merchant_id     integer   NOT NULL,
    approved        boolean   NOT NULL,
    -- how the answer was reached: 'db', 'cache', 'fallback_open',
    -- 'fallback_closed', 'kill_switch'
    source          text      NOT NULL,
    latency_us      integer   NOT NULL,
    decided_at_ms   bigint    NOT NULL
);

CREATE INDEX ON coverage.decision (run_id, guard, event_id);

-- The unique constraint variant. Applied to a SEPARATE table rather than as a
-- conditional index on the one above, because a partial unique index that
-- only applies to one guard is a construction a reader has to verify before
-- they can believe the result, and the point of the experiment is that the
-- database refuses the second write.
CREATE TABLE coverage.decision_unique (
    decision_id     bigserial PRIMARY KEY,
    run_id          text      NOT NULL,
    guard           text      NOT NULL,
    event_id        text      NOT NULL,
    member_id       integer   NOT NULL,
    merchant_id     integer   NOT NULL,
    approved        boolean   NOT NULL,
    source          text      NOT NULL,
    latency_us      integer   NOT NULL,
    decided_at_ms   bigint    NOT NULL,
    -- The whole experiment, in one line.
    UNIQUE (run_id, event_id)
);


-- ---------------------------------------------------------------------------
-- THE ANSWER KEY
-- ---------------------------------------------------------------------------

-- What a perfect decision would have been for every event, at the moment the
-- event happened. Generated, so "wrongly turned away" and "wrongly billed"
-- are countable rather than estimated.
CREATE TABLE truth.event (
    event_id        text    PRIMARY KEY,
    seq             integer NOT NULL,
    member_id       integer NOT NULL,
    merchant_id     integer NOT NULL,
    plan_id         text    NOT NULL,
    offered_at_ms   bigint  NOT NULL,
    -- The correct answer at offered_at_ms, which is not the same as the
    -- correct answer now. An event that arrives one millisecond before a
    -- status change has a different right answer than the one after it, and a
    -- truth table that stored only the final state would score a correct
    -- service as wrong.
    -- Redundant columns deliberately absent. Whether a delivery is a replay
    -- belongs to truth.arrival, not here: an EVENT is never a replay, a
    -- DELIVERY of it is. Carrying the flag in both places would let them
    -- disagree.
    covered         boolean NOT NULL
);

CREATE INDEX ON truth.event (seq);

-- ONE ROW PER DELIVERY, which is not one row per event. A replay carries the
-- original's event_id, so the arrival stream is longer than truth.event and
-- the two are kept apart deliberately: collapsing them would delete the
-- entire subject of experiment 4, and merging them is impossible against a
-- primary key.
CREATE TABLE truth.arrival (
    seq             integer PRIMARY KEY,
    event_id        text    NOT NULL REFERENCES truth.event,
    is_replay       boolean NOT NULL,
    replay_of       text
);

CREATE INDEX ON truth.arrival (event_id);
