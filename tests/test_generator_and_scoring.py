"""The traffic generator and the error accounting, offline.

The generator is the answer key. Every count of a wrong decision in this
repository is measured against it, so its invariants are what those counts are
worth.

The error accounting is tested separately from any run because the whole
argument of experiment 2 rests on the two error kinds being counted apart and
not confused with each other.
"""

import driver
import generate_traffic as gen


# ---------------------------------------------------------------------------
# DETERMINISM
# ---------------------------------------------------------------------------

def test_the_world_is_a_pure_function_of_the_seed():
    a = gen.manifest(*gen.build())
    b = gen.manifest(*gen.build())
    assert a["sha256"] == b["sha256"]


def test_nothing_in_the_generator_reads_the_clock():
    # A generator that reads the clock produces different data on every run
    # and makes every published number unreproducible. Checked by source
    # rather than by behavior: a clock read that only fires occasionally would
    # pass a behavioral check almost every time.
    import inspect
    src = inspect.getsource(gen)
    for forbidden in ("time.time", "datetime.now", "datetime.today",
                      "random.", "os.urandom"):
        assert forbidden not in src, forbidden


def test_two_different_coordinates_cannot_collide_into_one_draw():
    assert gen._bits("a", "bc") != gen._bits("ab", "c")


# ---------------------------------------------------------------------------
# THE WORLD
# ---------------------------------------------------------------------------

def test_the_arrival_stream_is_longer_than_the_set_of_distinct_events():
    # A replay is not a new event. If these were equal the repository would
    # have no replays and experiment 4 would be measuring nothing.
    _, _, _, stream, distinct = gen.build()
    assert len(stream) > len(distinct)
    assert len({e["event_id"] for e in stream}) == len(distinct)


def test_a_replay_carries_its_original_id_and_its_original_timestamp():
    # That is what a processor retry IS. Giving the replay a fresh timestamp
    # would make it a different authorization.
    _, _, _, stream, _ = gen.build()
    by_id = {}
    for e in stream:
        if not e["is_replay"]:
            by_id[e["event_id"]] = e
    replays = [e for e in stream if e["is_replay"]]
    assert replays
    for r in replays[:200]:
        original = by_id[r["event_id"]]
        assert r["offered_at_ms"] == original["offered_at_ms"]
        assert r["covered"] == original["covered"]


def test_arrival_timestamps_are_monotonic_for_first_deliveries():
    # (mutation-checked: draw the timestamp with _bits("at", i) % RUN_MS and
    # this fails, and experiment 3 starts scoring correct decisions as wrong
    # because an event's answer key predates the database state it met)
    _, _, _, stream, _ = gen.build()
    firsts = [e for e in stream if not e["is_replay"]]
    times = [e["offered_at_ms"] for e in firsts]
    assert times == sorted(times)


def test_a_merchant_that_is_not_a_provider_has_no_coverage_row_at_all():
    # Absence is the answer for the ordinary businesses a card also touches,
    # and the service has to treat a missing row as "not covered" rather than
    # as an error.
    _, merchants, status, _, _ = gen.build()
    non_providers = {m["merchant_id"] for m in merchants
                     if not m["is_provider"]}
    assert non_providers
    for row in status:
        assert row["merchant_id"] not in non_providers


def test_some_coverage_changes_land_inside_the_run_with_traffic_either_side():
    _, _, status, _, _ = gen.build()
    changes = [r for r in status if r["effective_from"] > 0]
    assert changes
    for c in changes:
        assert 0 < c["effective_from"] < gen.RUN_MS


def test_a_change_flips_the_answer_rather_than_restating_it():
    # A "change" to the same value would inflate the change count while being
    # invisible to any cache, and the staleness denominator would be wrong.
    _, _, status, _, _ = gen.build()
    by_pair = {}
    for r in status:
        by_pair.setdefault((r["plan_id"], r["merchant_id"]), []).append(r)
    checked = 0
    for rows in by_pair.values():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: r["effective_from"])
        assert rows[0]["covered"] != rows[1]["covered"]
        checked += 1
    assert checked > 0


def test_the_traffic_is_concentrated_on_a_minority_of_merchants():
    # Uniform merchant selection would make a cache useless by construction
    # and the TTL sweep in experiment 3 would be a flat line.
    man = gen.manifest(*gen.build())
    assert man["share_of_traffic_on_the_top_10_percent_of_merchants"] > 0.5


def test_coverage_at_an_instant_uses_the_latest_row_at_or_before_it():
    idx = {("P", 1): [{"covered": False, "effective_from": 0},
                      {"covered": True, "effective_from": 100}]}
    assert gen.coverage_at(idx, "P", 1, 0) is False
    assert gen.coverage_at(idx, "P", 1, 99) is False
    assert gen.coverage_at(idx, "P", 1, 100) is True
    assert gen.coverage_at(idx, "P", 1, 5000) is True


def test_a_pair_with_no_row_is_not_covered():
    assert gen.coverage_at({}, "P", 999, 5000) is False


# ---------------------------------------------------------------------------
# THE ERROR ACCOUNTING
# ---------------------------------------------------------------------------

def _rec(event_id, approved):
    return {"status": 200, "event_id": event_id, "approved": approved,
            "source": "db", "latency_us": 1}


def test_the_two_error_kinds_are_counted_apart():
    """(mutation-checked: swap the two branches and experiment 2's crossover
    inverts, recommending the opposite policy at every price)

    The fixture is deliberately asymmetric, two of one kind and one of the
    other, because that is what makes the mutation check true. With one error
    of each kind the two counters are both 1 whichever branch increments
    which, so swapping them is invisible and the advertised check passes on a
    swap that inverts the whole cost argument. On the shipped run the same
    swap moves 1,921 wrongly-denied to 1,921 wrongly-approved.
    """
    truth = {"a": {"covered": True}, "b": {"covered": True},
             "c": {"covered": False}}
    s = driver.score([_rec("a", False), _rec("b", False), _rec("c", True)],
                     truth)
    assert s["wrongly_denied"] == 2        # two covered members turned away
    assert s["wrongly_approved"] == 1      # one uncovered member billed
    assert s["correct"] == 0


def test_a_correct_decision_of_either_kind_counts_as_correct():
    truth = {"a": {"covered": True}, "b": {"covered": False}}
    s = driver.score([_rec("a", True), _rec("b", False)], truth)
    assert s["correct"] == 2
    assert s["accuracy"] == 1.0
    assert s["wrongly_denied"] == s["wrongly_approved"] == 0


def test_a_failed_request_is_not_scored_as_a_wrong_decision():
    # A driver-side failure is not a decision. Counting it as one would let a
    # broken harness report itself as a service that answers incorrectly.
    truth = {"a": {"covered": True}}
    s = driver.score([{"status": 0, "driver_error": "TimeoutError"}], truth)
    assert s["scored"] == 0
    assert s["accuracy"] is None


def test_a_decision_for_an_unknown_event_is_ignored_rather_than_guessed():
    s = driver.score([_rec("nobody", True)], {"a": {"covered": True}})
    assert s["scored"] == 0


# ---------------------------------------------------------------------------
# PERCENTILES
# ---------------------------------------------------------------------------

def test_the_percentile_is_a_value_that_actually_occurred():
    # Nearest rank, not interpolated. An interpolated percentile invents a
    # latency no request experienced, which matters here because a hard
    # timeout puts a cluster of requests at exactly the budget.
    values = [1, 2, 3, 4, 100]
    for p in (0, 1, 50, 95, 99, 100):
        assert driver.percentile(values, p) in values


def test_the_percentile_is_monotonic_and_bounded_by_the_data():
    values = sorted([5, 1, 9, 3, 7, 2, 8])
    seen = [driver.percentile(values, p) for p in range(0, 101, 5)]
    assert seen == sorted(seen)
    assert seen[0] == min(values) and seen[-1] == max(values)


def test_an_empty_sample_has_no_percentile_rather_than_a_zero():
    assert driver.percentile([], 99) is None
    assert driver.summarize([])["p99_us"] is None
