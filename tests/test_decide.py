"""The decision logic, as pure functions, with no database and no container.

Every rule that decides what a member is told is tested here. The plumbing in
service/db.py is not: it needs psycopg and a live Postgres, so it is exercised
by the experiments instead. What lives here is everything a reader would want
to disagree with.
"""

import pytest

from service.decide import (Cache, Config, SRC_CACHE, SRC_DB,
                            SRC_FALLBACK_CLOSED, SRC_FALLBACK_OPEN,
                            SRC_KILL_SWITCH, SRC_REPLAY, fallback_answer)


# ---------------------------------------------------------------------------
# THE FALLBACK POLICY
# ---------------------------------------------------------------------------

def test_fail_closed_denies_and_fail_open_approves():
    assert fallback_answer("closed") == (False, SRC_FALLBACK_CLOSED)
    assert fallback_answer("open") == (True, SRC_FALLBACK_OPEN)


def test_an_unknown_fallback_policy_raises_rather_than_defaulting():
    # (mutation-checked: return the closed policy instead of raising and a
    # typo in a config sweep silently measures one of the two policies the
    # experiment is comparing, producing a complete-looking run)
    with pytest.raises(ValueError):
        fallback_answer("fail-safe")
    with pytest.raises(ValueError):
        fallback_answer("")


def test_the_two_fallback_sources_are_distinguishable_in_the_record():
    # Every count in results/ is grouped by source. If both policies wrote the
    # same source string, no results file could say which one produced a
    # decision.
    assert SRC_FALLBACK_OPEN != SRC_FALLBACK_CLOSED
    assert len({SRC_DB, SRC_CACHE, SRC_FALLBACK_OPEN, SRC_FALLBACK_CLOSED,
                SRC_KILL_SWITCH, SRC_REPLAY}) == 6


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

def test_config_ignores_a_key_it_does_not_know():
    # The service must not grow settings by being sent them. A typo'd key
    # would otherwise be accepted, reported back, and do nothing.
    c = Config()
    c.update({"timeout_ms": 123, "tiemout_ms": 999})
    assert c.timeout_ms == 123
    assert not hasattr(c, "tiemout_ms")


def test_config_reports_every_field_it_holds():
    c = Config()
    assert set(c.as_dict()) == set(Config.FIELDS)


def test_the_budget_covers_the_queue_by_default():
    # The sound arrangement is the default and the naive one has to be asked
    # for. Experiment 1 measures both, but a reader who runs the service
    # without reading anything gets the one that bounds the tail.
    assert Config().budget_covers_queue is True


def test_the_kill_switch_answer_is_separate_from_the_fallback_policy():
    # They are different decisions: one is what to do when the dependency is
    # slow, the other is what to do when an operator has decided to stop
    # depending on it. Collapsing them would make the kill switch untestable
    # against a policy it does not share.
    c = Config()
    c.update({"fallback": "open", "kill_switch_answer": False})
    assert c.fallback == "open" and c.kill_switch_answer is False


# ---------------------------------------------------------------------------
# THE CACHE
# ---------------------------------------------------------------------------

class FakeClock:
    """A clock the test moves by hand.

    A test that proves an expiry by sleeping is slow, flaky, and proves it
    only for the one duration it waited.
    """

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_a_ttl_of_zero_disables_the_cache_entirely():
    clock = FakeClock()
    c = Cache(clock)
    c.put(("PLAN-GOLD", 1), True)
    assert c.get(("PLAN-GOLD", 1), 0) is None
    assert c.get(("PLAN-GOLD", 1), -1) is None


def test_an_entry_is_returned_inside_the_ttl_and_not_outside_it():
    clock = FakeClock()
    c = Cache(clock)
    c.put(("PLAN-GOLD", 1), True)
    clock.now = 999.0
    assert c.get(("PLAN-GOLD", 1), 1000) is True
    clock.now = 1001.0
    assert c.get(("PLAN-GOLD", 1), 1000) is None


def test_a_false_answer_is_cached_and_is_not_confused_with_a_miss():
    # (mutation-checked: test the entry with `if not value` instead of
    # `is None` and every denied merchant becomes a permanent cache miss,
    # which silently halves the hit rate in results/exp3.json)
    clock = FakeClock()
    c = Cache(clock)
    c.put(("PLAN-GOLD", 7), False)
    assert c.get(("PLAN-GOLD", 7), 1000) is False


def test_an_expiry_is_counted_apart_from_a_cold_miss():
    # They cost the same latency and mean completely different things: one is
    # a cache that has not warmed, the other is a TTL doing its job.
    clock = FakeClock()
    c = Cache(clock)
    c.get(("PLAN-GOLD", 1), 1000)              # cold
    c.put(("PLAN-GOLD", 1), True)
    clock.now = 5000.0
    c.get(("PLAN-GOLD", 1), 1000)              # expired
    s = c.stats()
    assert s["misses"] == 2
    assert s["expired"] == 1


def test_an_expired_entry_is_dropped_rather_than_left_to_be_re_expired():
    clock = FakeClock()
    c = Cache(clock)
    c.put(("PLAN-GOLD", 1), True)
    clock.now = 5000.0
    c.get(("PLAN-GOLD", 1), 1000)
    assert c.size() == 0


def test_the_hit_rate_is_a_share_and_survives_an_empty_cache():
    c = Cache(FakeClock())
    assert c.stats()["hit_rate"] == 0.0
    c.put(("P", 1), True)
    c.get(("P", 1), 1000)
    assert c.stats()["hit_rate"] == 1.0


def test_clearing_the_cache_resets_its_counters_too():
    # A sweep that cleared entries but kept counters would report the hit rate
    # of every previous sweep point added together.
    clock = FakeClock()
    c = Cache(clock)
    c.put(("P", 1), True)
    c.get(("P", 1), 1000)
    c.clear()
    assert c.size() == 0
    assert c.stats() == {"hits": 0, "misses": 0, "expired": 0, "hit_rate": 0.0}


def test_two_plans_at_the_same_merchant_are_different_cache_entries():
    # The key is (plan, merchant) and it has to be: the same provider is
    # in-network for one plan and out for another, and a merchant-only key
    # would serve one plan's answer to another's member.
    clock = FakeClock()
    c = Cache(clock)
    c.put(("PLAN-GOLD", 1), True)
    c.put(("PLAN-BRONZE", 1), False)
    assert c.get(("PLAN-GOLD", 1), 1000) is True
    assert c.get(("PLAN-BRONZE", 1), 1000) is False
