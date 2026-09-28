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

This file does not import `service/db.py`; it needs psycopg, and
tests/test_http_handler.py reaches it through a stub. What made testing `decide()` look impossible was that it takes a
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
    TimeoutError apart from any other exception, and folding them
    together would make a broken pool look like a blown latency budget.
    """

    def __init__(self, covered=True, raises=None):
        self.covered = covered
        self.raises = raises
        self.calls = 0
        # Every (plan_id, merchant_id) the datastore was asked about, in
        # order. The answer alone cannot show WHICH pair was looked up, and
        # two of the tests below are about exactly that.
        self.asked = []
        self.rows = {}          # (run_id, guard, event_id) -> standing answer
        self.inserts = 0

    def coverage(self, plan_id, merchant_id, **kw):
        self.calls += 1
        self.asked.append((plan_id, merchant_id))
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
    """The two are counted separately: folding them together makes
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


# ------------------------------------------------ what the cache WRITES
#
# The read side is covered above (expiry, the TTL of zero, two plans not
# sharing an answer), and the denial-caching claim in README.md is checked
# against `Cache.get` in tests/test_decide.py. None of that reaches the `put`
# in `decide`, and the write side is where the edits that keep every verdict
# right while collapsing experiment 3's hit rate live. The repository's
# documented workflow is "re-run, then update the README from it", so a
# collapsed hit rate would be published as a new measurement rather than
# noticed as a regression.

def test_a_denial_is_cached_like_any_other_answer():
    """(mutation-checked: `if approved: state.cache.put(...)` in
    service/decide.py leaves every verdict in this suite unchanged, because
    caching only changes WHERE an answer comes from. What it changes is
    experiment 3: about 68 percent of this traffic is denied, so denials not
    being cached takes most of the hit rate with it.)
    """
    pool = FakePool(covered=False)
    st = FakeState(pool, cache_ttl_ms=60000)
    first = decide(_req(), st)
    second = decide(_req("e2"), st)
    assert first["approved"] is False and first["source"] == SRC_DB
    assert second["approved"] is False, "a cached denial came back as approved"
    assert second["source"] == SRC_CACHE, "the denial was not cached"
    assert pool.calls == 1


def test_the_cache_key_is_the_plan_and_the_merchant_and_nothing_else():
    """(mutation-checked: add member_id to the key in service/decide.py and
    every verdict stays right while the hit rate collapses to whatever share
    of traffic is one member hitting one merchant twice.)

    The complement of `test_two_plans_at_the_same_merchant_do_not_share_a
    _cached_answer`, which pins what the key MUST separate. This pins what it
    must NOT: coverage is a property of a plan at a merchant, so two members
    on the same plan have to share the entry.
    """
    pool = FakePool(covered=True)
    st = FakeState(pool, cache_ttl_ms=60000)
    decide(_req("e1", member=1), st)
    out = decide(_req("e2", member=2), st)
    assert out["source"] == SRC_CACHE, "the member is in the cache key"
    assert pool.calls == 1
    # Different merchant, same plan and member: that IS a different entry.
    out = decide(_req("e3", member=1, merchant=99), st)
    assert out["source"] == SRC_DB
    assert pool.asked == [("GOLD", 1), ("GOLD", 99)]


def test_a_ttl_of_zero_writes_nothing_rather_than_writing_and_not_reading():
    """(mutation-checked: drop the `if cfg.cache_ttl_ms > 0` guard around the
    put and every other assertion in this suite still passes, because
    `Cache.get` returns None at a TTL of zero, so nothing is ever served from it. The entries
    accumulate unread for the life of the process, which is a leak rather than
    a wrong answer, and no verdict anywhere can see it.)
    """
    pool = FakePool(covered=True)
    st = FakeState(pool, cache_ttl_ms=0)
    for i in range(5):
        decide(_req("e%d" % i), st)
    assert pool.calls == 5
    assert st.cache.size() == 0, "the cache was written at a TTL of zero"


def test_the_kill_switch_does_not_so_much_as_consult_the_cache():
    """(mutation-checked: move the cache lookup above the kill-switch branch
    and this fails, where an assertion on `pool.calls` alone does not: a cache
    hit answers without the pool, so the request count is 1 either way and
    only the cache's own counters move.)

    A kill switch that consulted the cache would still be answering from a
    datastore, at one remove and with a staleness nobody chose.
    """
    pool = FakePool(covered=True)
    st = FakeState(pool, cache_ttl_ms=60000)
    decide(_req(), st)                       # one miss, then a put
    assert (st.cache.hits, st.cache.misses) == (0, 1)
    st.config.update({"kill_switch": True, "kill_switch_answer": False})
    out = decide(_req("e2"), st)
    assert out["source"] == SRC_KILL_SWITCH
    assert (st.cache.hits, st.cache.misses) == (0, 1), \
        "the kill switch read the cache on its way to answering"


# ------------------------------------- what the service does NOT know
#
# Pinned so that it stays a stated decision. service/db.py's `coverage`
# docstring and README.md's "Limits" section both describe this
# behavior; without a test, the description is the only thing holding it.

def test_an_unknown_plan_is_answered_not_covered_and_labelled_a_datastore_answer():
    """The coverage lookup is a scalar subquery, and a scalar subquery with no
    matching row returns NULL, which `bool()` makes False. So a plan id that
    exists nowhere gets the same answer as a plan that exists and does not
    cover this merchant, with the same `source`.
    """
    # A datastore that covers exactly one pair, and knows nothing else.
    pool = FakePool(covered=lambda plan, merchant:
                    (plan, merchant) == ("GOLD", 1))
    unknown = decide(_req(plan="PLAN-PLATINUM"), FakeState(pool))
    examined = decide(_req("e2", plan="GOLD", merchant=2), FakeState(pool))
    assert unknown["approved"] is False and unknown["source"] == SRC_DB
    # The pair that was looked up, and not only the answer that came back.
    # Without this the test passes against a decide() that ignores the
    # request's plan altogether and asks about a fixed one, because both arms
    # of the comparison below still come out False.
    assert pool.asked == [("PLAN-PLATINUM", 1), ("GOLD", 2)]
    # Indistinguishable, which is the finding rather than an incidental
    # equality: a caller has no way to tell an unexamined pair from an
    # examined negative.
    assert (unknown["approved"], unknown["source"]) == \
        (examined["approved"], examined["source"])


def test_the_source_vocabulary_has_no_value_for_nothing_to_examine():
    """The test above is a finding and not a nitpick. Every count in
    results/ is grouped by `source`, so adding a value is a change to the
    shape of the published evidence, and the absence of one for "no such plan"
    is what makes an unknown input arrive as a definite verdict.
    """
    from service import decide as decide_module
    sources = {v for k, v in vars(decide_module).items()
               if k.startswith("SRC_")}
    assert sources == {"db", "cache", "fallback_open", "fallback_closed",
                       "kill_switch", "replay"}


def test_the_plan_is_taken_from_the_request_and_never_from_the_member():
    """README.md says the service is not hardened. This is one of the things
    that means: `plan_id` arrives in the body, `coverage.member` is never
    consulted, and a caller may ask about any plan for any member.
    """
    pool = FakePool(covered=lambda plan, merchant: plan == "PLAN-PLATINUM")
    st = FakeState(pool)
    out = decide(_req(member=4242, plan="PLAN-PLATINUM"), st)
    assert out["approved"] is True
    # The lookup used the CALLER'S plan, with the member id nowhere in it.
    assert pool.asked == [("PLAN-PLATINUM", 1)]


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
