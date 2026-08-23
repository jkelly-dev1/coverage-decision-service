"""Invariants over the shipped results/*.json.

These are not regression tests against frozen latencies. This repository
measures timing, and timing moves between runs and between machines, so a test
that pinned a microsecond figure would fail for everybody who ran it. Each
test here states a property that has to hold of any correct run.

Where a test does constrain a number it constrains a RATIO or an ORDERING,
which is the part that reproduces. The absolute figures are in the README and
are re-derived from these same files by scripts/check_readme_numbers.py.
"""

import pytest


# ---------------------------------------------------------------------------
# EXPERIMENT 1: DEGRADATION
# ---------------------------------------------------------------------------

def test_the_noop_floor_is_far_cheaper_than_a_real_decision(exp1):
    # (mutation-checked: remove disable_nagle_algorithm from the handler and
    # the floor becomes 42 ms against a 1 ms database query, which inflated
    # the bound until a refuted prediction read as held)
    floor = exp1["noop_floor"]
    fastest_real = min(r["p50_us"] for r in exp1["budget_covers_queue"])
    assert floor["p50_us"] < fastest_real
    assert floor["errors"] == 0


def test_the_floor_is_published_at_all(exp1):
    # Every other figure in this experiment is only meaningful above it, and a
    # results file without it would make them unreadable.
    for key in ("p50_us", "p95_us", "p99_us", "max_us"):
        assert exp1["noop_floor"][key] is not None


def test_nothing_falls_back_when_the_datastore_is_not_slowed(exp1):
    # The control. At zero injected delay the datastore answers well inside
    # the budget, so almost every decision should be a real one.
    zero = [r for r in exp1["budget_covers_queue"]
            if r["inject_db_delay_ms"] == 0][0]
    assert zero["share_answered_from_the_datastore"] > 0.85


def test_everything_falls_back_once_the_delay_exceeds_the_budget(exp1):
    for arrangement in ("budget_covers_queue", "statement_timeout_only"):
        worst = [r for r in exp1[arrangement]
                 if r["inject_db_delay_ms"] == max(exp1["injected_delays_ms"])][0]
        assert worst["share_answered_from_the_datastore"] == 0.0


def test_the_share_of_real_answers_never_rises_as_the_datastore_slows(exp1):
    for arrangement in ("budget_covers_queue", "statement_timeout_only"):
        rows = sorted(exp1[arrangement], key=lambda r: r["inject_db_delay_ms"])
        shares = [r["share_answered_from_the_datastore"] for r in rows]
        assert shares == sorted(shares, reverse=True)


def test_a_budget_that_covers_the_queue_bounds_the_tail_far_better(exp1):
    # The headline, and it is a ratio on purpose. The absolute verdict sits
    # near the boundary and moves between runs; the ratio between the two
    # arrangements does not.
    assert exp1["tail_ratio_naive_over_covered"] > 4.0


def test_the_same_timeout_is_configured_for_both_arrangements(exp1):
    # Otherwise the comparison would be between two different budgets and
    # would say nothing about where the budget is spent.
    assert exp1["timeout_ms"] == 50
    assert len(exp1["budget_covers_queue"]) == len(exp1["statement_timeout_only"])


def test_the_experiment_ran_above_pool_saturation(exp1):
    # Below saturation the two arrangements are indistinguishable, so a run at
    # low concurrency would report that the distinction does not matter.
    assert exp1["concurrency"] > exp1["pool_size"]


def test_shedding_load_sustains_more_throughput_than_queueing_it(exp1):
    # A second finding, and one that is easy to miss: the arrangement that
    # gives up earlier also serves more requests per second under overload,
    # because it stops holding connections for callers who have already been
    # answered.
    worst_delay = max(exp1["injected_delays_ms"])
    covered = [r for r in exp1["budget_covers_queue"]
               if r["inject_db_delay_ms"] == worst_delay][0]
    naive = [r for r in exp1["statement_timeout_only"]
             if r["inject_db_delay_ms"] == worst_delay][0]
    assert covered["requests_per_second"] > naive["requests_per_second"]


def test_no_driver_side_errors_are_hidden_in_the_latency_figures(exp1):
    for arrangement in ("budget_covers_queue", "statement_timeout_only"):
        for row in exp1[arrangement]:
            assert row["driver_errors"] == 0
            assert row["http_errors"] == 0


# ---------------------------------------------------------------------------
# EXPERIMENT 2: fail open or fail closed
# ---------------------------------------------------------------------------

def test_the_two_policies_agree_when_nothing_falls_back(exp2):
    # The control. At zero injected delay the fallback policy is barely
    # consulted, so the two runs must be close. They are not identical,
    # concurrency times a few requests out either way, and the results
    # record the gap.
    assert exp2["control_gap_decisions_at_zero_delay"] < exp2["requests"] * 0.05


def test_fail_closed_never_bills_anybody_and_fail_open_never_turns_anybody_away(
        exp2):
    # At full degradation every decision is a fallback, so each policy makes
    # exactly one kind of error and none of the other.
    worst = max(exp2["injected_delays_ms"])
    closed = [r for r in exp2["runs"] if r["inject_db_delay_ms"] == worst
              and r["fallback"] == "closed"][0]
    opened = [r for r in exp2["runs"] if r["inject_db_delay_ms"] == worst
              and r["fallback"] == "open"][0]
    assert closed["wrongly_approved"] == 0
    assert opened["wrongly_denied"] == 0


def test_the_measured_crossover_matches_the_analytic_one(exp2):
    # At full degradation the answer is arithmetic, not a measurement. Every
    # decision is a fallback, so fail-open wins exactly while price <
    # base_rate / (1 - base_rate). This is the one result in the repository
    # that does not depend on the machine it ran on.
    worst = max(exp2["injected_delays_ms"])
    analytic = exp2["analytic_crossover_price"]
    for row in exp2["priced"]:
        if row["inject_db_delay_ms"] != worst:
            continue
        price = row["price_of_a_bill_in_turnaways"]
        if price < analytic:
            assert row["winner"] == "open"
        elif price > analytic:
            assert row["winner"] == "closed"


def test_the_crossover_is_a_function_of_the_base_rate_alone(exp2):
    # The tolerance is absolute and loose on purpose. Both figures are stored
    # rounded to six places, so reconstructing either from the other cannot
    # agree beyond that; a tighter tolerance would be testing the rounding.
    base = exp2["base_coverage_rate"]
    assert exp2["analytic_crossover_price"] == pytest.approx(
        base / (1 - base), abs=1e-5)


def test_the_winning_policy_flips_inside_the_swept_price_range(exp2):
    # (mutation-checked: sweep only prices above 1.0 and the repository
    # concludes that fail-closed is simply correct, which is the conclusion it
    # exists to argue against)
    assert exp2["prediction"]["verdict"] == "held"
    assert len(exp2["prediction"]["distinct_winners"]) > 1


def test_degradation_never_improves_accuracy(exp2):
    for policy in ("closed", "open"):
        rows = sorted((r for r in exp2["runs"] if r["fallback"] == policy),
                      key=lambda r: r["inject_db_delay_ms"])
        assert rows[0]["accuracy"] >= rows[-1]["accuracy"]


# ---------------------------------------------------------------------------
# EXPERIMENT 3: the cache and the kill switch
# ---------------------------------------------------------------------------

def test_the_staleness_denominator_is_the_affected_decisions_not_all_of_them(
        exp3):
    # Reporting stale errors against all 12,000 decisions would divide a real
    # effect by a number chosen to make it look small.
    for row in exp3["ttl_sweep"]:
        assert row["affected_decisions"] > 0
        assert row["affected_decisions"] < exp3["requests"]
        assert row["affected_wrong"] <= row["affected_decisions"]


def test_a_longer_ttl_never_lowers_the_hit_rate(exp3):
    rows = sorted(exp3["ttl_sweep"], key=lambda r: r["cache_ttl_ms"])
    hits = [r["cache_hit_share"] for r in rows]
    assert hits == sorted(hits)


def test_a_longer_ttl_never_reduces_the_staleness_error(exp3):
    rows = sorted(exp3["ttl_sweep"], key=lambda r: r["cache_ttl_ms"])
    errs = [r["affected_error_rate"] for r in rows]
    assert errs == sorted(errs)


def test_the_cache_is_off_at_a_ttl_of_zero(exp3):
    zero = [r for r in exp3["ttl_sweep"] if r["cache_ttl_ms"] == 0][0]
    assert zero["cache_hit_share"] == 0.0


def test_nothing_fell_back_during_the_cache_experiment(exp3):
    # The timeout is deliberately generous here. A run where requests were
    # also falling back would confound the cache with the budget.
    for row in exp3["ttl_sweep"]:
        assert row["fell_back"] == 0


def test_the_price_of_a_point_of_hit_rate_rises_sharply_with_the_ttl(exp3):
    # The finding. The averages hide the shape; what decides a TTL is what the
    # next increment costs.
    priced = [s for s in exp3["marginal_steps"]
              if s["error_added_per_point_of_hit_rate"] is not None
              and s["hit_rate_gained"] > 0.01]
    assert len(priced) >= 3
    assert priced[-1]["error_added_per_point_of_hit_rate"] > \
        priced[0]["error_added_per_point_of_hit_rate"] * 10


def test_a_short_ttl_buys_hit_rate_for_nothing(exp3):
    # Refutes the second prediction, which said every gain in hit rate is paid
    # for. It is not: the first step is free.
    assert exp3["largest_free_ttl_ms"] > 0
    assert exp3["hit_rate_at_the_largest_free_ttl"] > 0.1
    assert exp3["predictions"]["no_ttl_buys_hit_rate_for_free"]["verdict"] \
        == "REFUTED"


def test_latency_never_argues_for_a_shorter_ttl(exp3):
    assert exp3["predictions"]["longer_is_better_for_latency"]["verdict"] \
        == "held"


def test_the_kill_switch_answers_without_touching_the_datastore(exp3):
    # A kill switch that still consulted the cache or the pool would not be
    # one. Its latency has to be far below anything that does.
    slowest_normal = max(r["p50_us"] for r in exp3["ttl_sweep"])
    for row in exp3["kill_switch"]:
        assert row["p50_us"] * 100 < slowest_normal


def test_what_the_kill_switch_costs_is_measured_and_not_asserted(exp3):
    # "We have a kill switch" is a claim about configuration. This is the
    # measurement: flipping it changes this many decisions, in this direction.
    deny, approve = exp3["kill_switch"]
    assert deny["kill_switch_answer"] is False
    assert approve["kill_switch_answer"] is True
    assert deny["share_of_decisions_changed"] > 0.1
    assert approve["share_of_decisions_changed"] > 0.1
    # The two must account for every decision between them: one flips exactly
    # the approvals, the other exactly the denials.
    assert deny["share_of_decisions_changed"] + \
        approve["share_of_decisions_changed"] == pytest.approx(1.0, abs=0.02)


# ---------------------------------------------------------------------------
# EXPERIMENT 4: IDEMPOTENCY
# ---------------------------------------------------------------------------

def _guard(rows, name):
    return [r for r in rows if r["guard"] == name][0]


def test_no_guard_writes_one_row_per_delivery(exp4):
    for workload in ("spread", "burst"):
        row = _guard(exp4[workload]["rows"], "none")
        assert row["rows_written"] == row["deliveries"]


def test_the_unique_constraint_writes_exactly_one_row_per_event(exp4):
    for workload in ("spread", "burst"):
        row = _guard(exp4[workload]["rows"], "unique")
        assert row["duplicate_rows"] == 0
        assert row["rows_written"] == row["distinct_events_delivered"]
        assert row["events_with_disagreeing_decisions"] == 0


def test_check_then_insert_is_not_idempotent(exp4):
    # The result. It is a race, and a race does not need to fire often to be
    # a defect.
    assert exp4["prediction"]["verdict"] == "REFUTED"
    assert _guard(exp4["burst"]["rows"], "check_then_insert")["duplicate_rows"] > 0


def test_check_then_insert_removes_most_duplicates_without_removing_them_all(
        exp4):
    # The worst kind of guarantee: it survives every test anybody writes by
    # hand and fails at volume.
    assert exp4["duplicates_removed_by_check_then_insert_under_burst"] > 0.9
    assert exp4["duplicates_removed_by_check_then_insert_under_burst"] < 1.0


def test_the_unguarded_run_leaves_contradictory_decisions_on_record(exp4):
    # A decision log holding both "approved" and "denied" for one
    # authorization cannot be reconciled by anybody afterward.
    row = _guard(exp4["burst"]["rows"], "none")
    assert row["events_with_disagreeing_decisions"] > 0


def test_the_burst_actually_mixed_real_answers_with_fallbacks(exp4):
    # If every delivery had reached the datastore, the duplicates would all
    # agree and the dangerous case would not appear at all. This asserts the
    # workload did what it was designed to do.
    sources = _guard(exp4["burst"]["rows"], "none")["by_source"]
    assert sources.get("db", 0) > 0
    assert sources.get("fallback_closed", 0) > 0


def test_every_delivery_got_an_answer_under_every_guard(exp4):
    # Returning the original decision is the requirement, not refusing the
    # replay. A guard that raised on the second delivery would show up here.
    for workload in ("spread", "burst"):
        for row in exp4[workload]["rows"]:
            assert row["http_errors"] == 0


def test_a_spread_replay_is_recognized_even_by_the_racy_guard(exp4):
    # The contrast that makes the finding: the same guard looks correct when
    # replays are far apart and fails when they are simultaneous.
    spread_row = _guard(exp4["spread"]["rows"], "check_then_insert")
    assert spread_row["responses_marked_replayed"] > 0
    assert spread_row["duplicate_rows"] < spread_row["replay_deliveries"] * 0.1


# ---------------------------------------------------------------------------
# Every result names the input it was measured on
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["exp1", "exp2", "exp3", "exp4"])
def test_every_result_carries_the_manifest_of_its_input(name, request):
    data = request.getfixturevalue(name)
    assert len(data["input_manifest_sha256"]) == 64


def test_all_four_experiments_measured_the_same_generated_world(
        exp1, exp2, exp3, exp4):
    shas = {d["input_manifest_sha256"] for d in (exp1, exp2, exp3, exp4)}
    assert len(shas) == 1
