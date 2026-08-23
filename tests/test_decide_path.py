"""`decide()` itself; the function every request goes through.

Why this file exists. `tests/conftest.py` and the CI workflow both said the
decision logic "is tested in full". It was not: `tests/test_decide.py` imports
`Cache`, `Config`, `fallback_answer` and the source constants, and never
imports `decide`. Lines 177-248 of `service/decide.py`, the whole body that
composes those pieces into an answer, were executed by nothing.

The consequences were not theoretical. With that gap, all of the following
left the suite green:

    the kill switch made a no-op, and made to fire always
    the timeout raised to 10**9 ms, so the fallback path stopped existing
    the cache TTL raised to 10**12 ms, so a wrong answer is served forever
    the cache key reduced to merchant_id, so two plans share an answer
    the check-then-insert replay guard disabled, so a webhook counts twice
    the unique-constraint replay marked as a fresh decision

`service/db.py` still is not imported; it needs psycopg and that exclusion is
deliberate. What made testing `decide()` look impossible was that it takes a
live pool. It does not: it takes something with `.coverage()` and
`.record()`, which is thirty lines of fake.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.decide import (Cache, Config, GUARD_CHECK_THEN_INSERT,   # noqa: E402
                            GUARD_NONE, GUARD_UNIQUE, SRC_CACHE, SRC_DB,
                            SRC_FALLBACK_CLOSED, SRC_FALLBACK_OPEN,
                            SRC_KILL_SWITCH, SRC_REPLAY, decide)


class FakePool:
    """A datastore that answers from a dict, on demand, with a chosen failure.

    `raises` is the shape of the failure, which matters: `decide()` counts a
    TimeoutError apart from any other exception on purpose, and folding them
    together would make a broken pool look like a blown latency budget.
    """

    def __init__(self, covered=True, raises=None):
        self.covered = covered
        self.raises = raises
        self.calls = 0
        self.rows = {}          # (run_id, guard, event_id) -> standing answer
        self.inserts = 0

    def coverage(self, plan_id, merchant_id, **kw):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        if callable(self.covered):
            return self.covered(plan_id, merchant_id)
        return self.covered

    def prior_decision(self, run_id, guard, event_id):
        key = (run_id, guard, event_id)
        return {"approved": self.rows[key]} if key in self.rows else None

    def record(self, cfg, event_id, member_id, merchant_id, approved, source, us):
        """The unique constraint only exists under the unique guard, which is
        the whole comparison: under `none` the insert always succeeds and the
        same webhook is decided twice."""
        key = (cfg.run_id, cfg.guard, event_id)
        if cfg.guard == GUARD_UNIQUE and key in self.rows:
            return False, self.rows[key]        # On conflict do nothing
        if key not in self.rows:
            self.rows[key] = approved
        self.inserts += 1
        return True, approved


class FakeState:
    def __init__(self, pool, **cfg):
        self.config = Config()
        self.config.update(cfg)
        self.cache = Cache()
        self.pool = pool
        self.counters = {}

    def bump(self, name, n=1):
        self.counters[name] = self.counters.get(name, 0) + n


def _req(event_id="e1", member=1, merchant=1, plan="GOLD"):
    return {"event_id": event_id, "member_id": member,
            "merchant_id": merchant, "plan_id": plan}


# ------------------------------------------------------------ kill switch

def test_the_kill_switch_answers_without_touching_the_datastore():
    pool = FakePool(covered=True)
    st = FakeState(pool, kill_switch=True, kill_switch_answer=False)
    out = decide(_req(), st)
    assert out["source"] == SRC_KILL_SWITCH
    assert out["approved"] is False
    assert pool.calls == 0, "the kill switch consulted the datastore"


def test_the_kill_switch_answer_is_the_one_configured():
    st = FakeState(FakePool(covered=False), kill_switch=True,
                   kill_switch_answer=True)
    assert decide(_req(), st)["approved"] is True


def test_with_the_switch_off_the_datastore_is_consulted():
    pool = FakePool(covered=True)
    st = FakeState(pool, kill_switch=False)
    out = decide(_req(), st)
    assert out["source"] == SRC_DB and pool.calls == 1


# --------------------------------------------------------------- fallback

def test_a_timeout_falls_back_and_is_counted_as_a_timeout():
    st = FakeState(FakePool(raises=TimeoutError("budget")), fallback="closed")
    out = decide(_req(), st)
    assert out["source"] == SRC_FALLBACK_CLOSED and out["approved"] is False
    assert st.counters.get("timeout") == 1
    assert "db_error" not in st.counters


def test_an_error_falls_back_and_is_counted_apart_from_a_timeout():
    """The two are counted separately on purpose: folding them together makes
    a broken dependency look like a blown latency budget."""
    st = FakeState(FakePool(raises=ConnectionResetError("gone")), fallback="closed")
    out = decide(_req(), st)
    assert out["source"] == SRC_FALLBACK_CLOSED
    assert st.counters.get("db_error") == 1
    assert "timeout" not in st.counters


def test_fail_open_approves_where_fail_closed_denies():
    a = decide(_req(), FakeState(FakePool(raises=TimeoutError()), fallback="open"))
    b = decide(_req(), FakeState(FakePool(raises=TimeoutError()), fallback="closed"))
    assert (a["approved"], a["source"]) == (True, SRC_FALLBACK_OPEN)
    assert (b["approved"], b["source"]) == (False, SRC_FALLBACK_CLOSED)


# ------------------------------------------------------------------ cache

def test_a_second_request_is_served_from_the_cache():
    pool = FakePool(covered=True)
    st = FakeState(pool, cache_ttl_ms=60000)
    first, second = decide(_req(), st), decide(_req("e2"), st)
    assert first["source"] == SRC_DB and second["source"] == SRC_CACHE
    assert pool.calls == 1


def test_a_zero_ttl_disables_the_cache_entirely():
    pool = FakePool(covered=True)
    st = FakeState(pool, cache_ttl_ms=0)
    decide(_req(), st)
    decide(_req("e2"), st)
    assert pool.calls == 2 and st.counters.get("cache_hit") is None


def test_the_cache_expires_rather_than_serving_a_stale_answer_forever():
    """A TTL that never expires turns one stale read into a permanently wrong
    answer, and the hit rate goes UP while it happens."""
    answers = {"n": 0}

    def flipping(plan, merchant):
        answers["n"] += 1
        return answers["n"] == 1          # True first, False thereafter

    pool = FakePool(covered=flipping)
    st = FakeState(pool, cache_ttl_ms=1)
    assert decide(_req(), st)["approved"] is True
    import time as _t
    _t.sleep(0.01)                         # past a 1 ms TTL
    out = decide(_req("e2"), st)
    assert out["source"] == SRC_DB, "the entry outlived its TTL"
    assert out["approved"] is False


def test_two_plans_at_the_same_merchant_do_not_share_a_cached_answer():
    """The key is (plan, merchant). Keyed on merchant alone, a plan that does
    not cover it is served the other plan's approval."""
    pool = FakePool(covered=lambda plan, merchant: plan == "GOLD")
    st = FakeState(pool, cache_ttl_ms=60000)
    assert decide(_req(plan="GOLD"), st)["approved"] is True
    out = decide(_req("e2", plan="BRONZE"), st)
    assert out["approved"] is False, "BRONZE was served GOLD's cached answer"


# ------------------------------------------------------------ idempotency

def test_without_a_guard_a_replayed_webhook_is_decided_twice():
    pool = FakePool(covered=True)
    st = FakeState(pool, record=True, guard=GUARD_NONE)
    a, b = decide(_req("dup"), st), decide(_req("dup"), st)
    assert a["replayed"] is False and b["replayed"] is False
    assert pool.inserts == 2, "the control arm must double-count"


def test_check_then_insert_returns_the_original_decision_on_replay():
    pool = FakePool(covered=True)
    st = FakeState(pool, record=True, guard=GUARD_CHECK_THEN_INSERT)
    a, b = decide(_req("dup"), st), decide(_req("dup"), st)
    assert a["replayed"] is False
    assert b["replayed"] is True and b["source"] == SRC_REPLAY
    assert pool.inserts == 1


def test_the_unique_constraint_returns_the_standing_answer_on_replay():
    pool = FakePool(covered=True)
    st = FakeState(pool, record=True, guard=GUARD_UNIQUE)
    a, b = decide(_req("dup"), st), decide(_req("dup"), st)
    assert a["replayed"] is False
    assert b["replayed"] is True and b["source"] == SRC_REPLAY
    assert pool.inserts == 1


def test_a_replay_returns_the_ORIGINAL_answer_not_a_fresh_one():
    """Idempotency has to return the original answer, not one row. Detected
    by whether a row was inserted rather than by comparing the two answers,
    because most replays would agree by chance."""
    answers = {"n": 0}

    def flipping(plan, merchant):
        answers["n"] += 1
        return answers["n"] == 1

    pool = FakePool(covered=flipping)
    st = FakeState(pool, record=True, guard=GUARD_UNIQUE, cache_ttl_ms=0)
    first = decide(_req("dup"), st)
    second = decide(_req("dup"), st)
    assert first["approved"] is True
    assert second["approved"] is True, "the replay was answered afresh"
    assert second["replayed"] is True


# ------------------------------------------------------------ branch order

def test_the_kill_switch_is_checked_before_the_cache_and_the_cache_before_the_db():
    """A kill switch that still consulted the cache would not be a kill
    switch, and a cache checked after the datastore would not be a cache."""
    pool = FakePool(covered=True)
    st = FakeState(pool, cache_ttl_ms=60000)
    decide(_req(), st)                       # warm the cache
    assert pool.calls == 1
    st.config.update({"kill_switch": True, "kill_switch_answer": False})
    out = decide(_req("e2"), st)
    assert out["source"] == SRC_KILL_SWITCH
    assert pool.calls == 1, "the kill switch fell through to the datastore"
