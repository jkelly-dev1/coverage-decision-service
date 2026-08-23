"""Re-derive every published figure from results/*.json and require it verbatim
in README.md.

    python3 scripts/check_readme_numbers.py

Why this exists. A number in a document has no owner. The results files are
rewritten by every run; the prose is rewritten by hand, sometimes, when
somebody remembers. This script makes the prose fail instead of drift.

It covers sentences, not only table rows. A percentage inside a paragraph does
not look like a figure to a reader or to whoever writes a deriver, so it is
the one most likely to go stale.

It prints how many figures it checked whether or not any are missing, so a
version of this file that has quietly stopped deriving half of them is visible
rather than clean.

What it does not do. It compares the README against the COMMITTED evidence, not
against a fresh run. This repository measures timing, so re-running WILL move
the microsecond figures and this script WILL go red; the README is then what
has to be updated. That is the intended workflow and not a defect.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lab

README = os.path.join(lab.REPO, "README.md")


def n(x):
    return "{:,}".format(int(x))


def f(x, places):
    return "%.*f" % (places, float(x))


def pct(x, places=2):
    return "%.*f" % (places, float(x) * 100.0)


def build():
    e1 = lab.read_result("exp1_degradation")
    e2 = lab.read_result("exp2_fail_open_closed")
    e3 = lab.read_result("exp3_cache_and_kill_switch")
    e4 = lab.read_result("exp4_idempotency")
    want = []

    def add(label, s):
        want.append((label, s))

    # ---- experiment 1 ------------------------------------------------------
    fl = e1["noop_floor"]
    add("floor row", "| %s us | %s us | %s us | %s us | %s rps |"
        % (n(fl["p50_us"]), n(fl["p95_us"]), n(fl["p99_us"]), n(fl["max_us"]),
           n(fl["requests_per_second"])))
    add("concurrency and pool", "concurrency %d against a pool of %d"
        % (e1["concurrency"], e1["pool_size"]))
    add("requests per point", "%s requests at concurrency" % n(e1["requests_per_point"]))
    add("timeout in prose", "same %d ms budget" % e1["timeout_ms"])

    cov = {r["inject_db_delay_ms"]: r for r in e1["budget_covers_queue"]}
    nai = {r["inject_db_delay_ms"]: r for r in e1["statement_timeout_only"]}
    for d in (0, 10, 25, 40, 60, 200):
        add("degradation row %d" % d, "| %d ms | %s us | %s us | %s us | %s us |"
            % (d, n(cov[d]["p50_us"]), n(cov[d]["p99_us"]),
               n(nai[d]["p50_us"]), n(nai[d]["p99_us"])))

    p = e1["prediction"]
    add("worst p99 both ways",
        "The worst p99 is %s us when the budget covers the queue and %s us when"
        % (n(p["with_budget_covering_the_queue"]["worst_p99_us"]),
           n(p["with_a_statement_timeout_alone"]["worst_p99_us"])))
    add("tail ratio in section 1", "a tail %s times longer"
        % f(e1["tail_ratio_naive_over_covered"], 1))
    add("tail ratio in the summary", "tail %s times longer"
        % f(e1["tail_ratio_naive_over_covered"], 1))
    add("reference bound", "(%s us), the sound arrangement lands at %s of it "
        "and the naive one at %s"
        % (n(p["with_budget_covering_the_queue"]["bound_us"]),
           f(p["with_budget_covering_the_queue"]["worst_p99_over_bound"], 2),
           f(p["with_a_statement_timeout_alone"]["worst_p99_over_bound"], 2)))
    add("real answers at zero delay", "only %s percent of requests got an answer"
        % pct(cov[0]["share_answered_from_the_datastore"]))
    add("real answers at 25 ms", "answers %s percent of requests from the "
        "datastore" % pct(cov[25]["share_answered_from_the_datastore"]))
    add("throughput contrast", "serves %s rps and the naive one %s"
        % (n(cov[200]["requests_per_second"]), n(nai[200]["requests_per_second"])))

    # ---- the world ---------------------------------------------------------
    add("events affected", "the %d decisions whose" % e3["events_affected_by_a_change"])
    # Derive from what was applied, not from the world total. The world holds
    # 194 changes; the replayed window applies 130, and every ttl_sweep row
    # records that. Deriving the sentence from the total certified a claim the
    # results file contradicts.
    applied = sorted({r["changes_applied"] for r in e3["ttl_sweep"]})
    assert len(applied) == 1, "ttl sweep rows disagree on changes_applied"
    add("coverage changes",
        "The world holds %d coverage changes and %d of\nthem fall inside the "
        "replayed window and are applied"
        % (e3["coverage_changes"], applied[0]))

    # ---- experiment 2 ------------------------------------------------------
    add("exp2 population", "%s distinct events, %s of them covered"
        % (n(e2["requests"]), n(e2["covered_events"])))
    # Named for what it is. The recorded figure is |errors_closed -
    # errors_open|, a difference of TOTALS, and calling it a count of
    # differing decisions overstates what was measured: the two runs fall back
    # on different requests, so it is a lower bound on the disagreement.
    add("control gap", "error TOTALS differ by only %s -- %s against %s"
        % (n(e2["control_gap_decisions_at_zero_delay"]),
           n(runs_zero_closed_errors := sum(
               runs0["wrongly_denied"] + runs0["wrongly_approved"]
               for runs0 in e2["runs"]
               if runs0["inject_db_delay_ms"] == 0 and runs0["fallback"] == "closed")),
           n(sum(runs0["wrongly_denied"] + runs0["wrongly_approved"]
                 for runs0 in e2["runs"]
                 if runs0["inject_db_delay_ms"] == 0 and runs0["fallback"] == "open"))))
    runs = {(r["inject_db_delay_ms"], r["fallback"]): r for r in e2["runs"]}
    for d in (0, 25, 60, 200):
        for pol in ("closed", "open"):
            r = runs[(d, pol)]
            add("exp2 row %d %s" % (d, pol),
                "| %d ms | %s | %s%% | %s | %s | %s |"
                % (d, pol, pct(r["share_that_fell_back"]),
                   n(r["wrongly_denied"]), n(r["wrongly_approved"]),
                   f(r["accuracy"], 4)))
    worst = max(e2["injected_delays_ms"])
    for row in e2["priced"]:
        if row["inject_db_delay_ms"] != worst:
            continue
        price = row["price_of_a_bill_in_turnaways"]
        add("price row %s" % price, "| %s | %s | %s | fail-%s |"
            % (f(price, 2), n_or_f(row["cost_fail_closed"]),
               n_or_f(row["cost_fail_open"]), row["winner"]))
    add("crossover in prose", "the crossover is at %s"
        % f(e2["analytic_crossover_price"], 4))
    add("crossover arithmetic", "= %s / %s = %s"
        % (f(e2["base_coverage_rate"], 4), f(1 - e2["base_coverage_rate"], 4),
           f(e2["analytic_crossover_price"], 4)))
    add("crossover in the summary", "less than %s of a member turned away"
        % f(e2["analytic_crossover_price"], 2))

    # ---- experiment 3 ------------------------------------------------------
    add("exp3 population", "%s events with a %d ms datastore delay"
        % (n(e3["requests"]), e3["injected_db_delay_ms"]))
    add("exp3 timeout", "generous %d ms timeout" % e3["timeout_ms"])
    ttl_label = {0: "none", 250: "250 ms", 1000: "1 s", 5000: "5 s",
                 30000: "30 s", 300000: "300 s"}
    for r in e3["ttl_sweep"]:
        add("ttl row %d" % r["cache_ttl_ms"],
            "| %s | %s | %s us | %s | %s (%s) |"
            % (ttl_label[r["cache_ttl_ms"]], f(r["cache_hit_share"], 4),
               n(r["p50_us"]), f(r["accuracy"], 4), n(r["affected_wrong"]),
               f(r["affected_error_rate"], 4)))
    step_label = {(0, 250): "none to 250 ms", (250, 1000): "250 ms to 1 s",
                  (1000, 5000): "1 s to 5 s", (5000, 30000): "5 s to 30 s",
                  (30000, 300000): "30 s to 300 s"}
    for st in e3["marginal_steps"]:
        key = (st["from_ttl_ms"], st["to_ttl_ms"])
        # A step that gains no hit rate has no price per point. It is
        # undefined rather than zero, and whether the last step gains a
        # hundredth of a percent or nothing at all varies between runs, so
        # both the results file and the README have to render it as such.
        per = st["error_added_per_point_of_hit_rate"]
        add("marginal row %s" % (key,), "| %s | %+.4f | %+.4f | %s |"
            % (step_label[key], st["hit_rate_gained"],
               st["affected_error_added"],
               "n/a" if per is None else f(per, 4)))
    # The multiple between the cheapest and dearest step is deliberately not
    # derived. It is a ratio against a denominator near zero, it moved by a
    # factor of two between two runs of the same experiment, and publishing it
    # would be publishing noise. The dearest step's own value is stable and is
    # what the README quotes.
    steps = [s for s in e3["marginal_steps"]
             if s["error_added_per_point_of_hit_rate"]
             and s["hit_rate_gained"] > 0.01]
    add("dearest step", "costs %s error per"
        % f(steps[-1]["error_added_per_point_of_hit_rate"], 4))
    add("free ttl", "%d ms TTL buys a %s percent hit rate"
        % (e3["largest_free_ttl_ms"],
           pct(e3["hit_rate_at_the_largest_free_ttl"])))
    add("saturation ttl", "past %s nothing changes"
        % ttl_label[e3["hit_rate_saturates_at_ttl_ms"]])
    deny, approve = e3["kill_switch"]
    add("kill switch deny", "| deny everything | %s us | %s rps | %s (%s%%) | %s |"
        % (n(deny["p50_us"]), n(deny["requests_per_second"]),
           n(deny["decisions_changed_vs_normal"]),
           pct(deny["share_of_decisions_changed"]), f(deny["accuracy"], 4)))
    add("kill switch approve",
        "| approve everything | %s us | %s rps | %s (%s%%) | %s |"
        % (n(approve["p50_us"]), n(approve["requests_per_second"]),
           n(approve["decisions_changed_vs_normal"]),
           pct(approve["share_of_decisions_changed"]),
           f(approve["accuracy"], 4)))

    # ---- experiment 4 ------------------------------------------------------
    label = {"none": "none", "check_then_insert": "check-then-insert",
             "unique": "unique constraint"}
    for workload in ("spread", "burst"):
        for row in e4[workload]["rows"]:
            add("%s row %s" % (workload, row["guard"]),
                "| %s | %s | %s | %s | %s | %s |"
                % (label[row["guard"]], n(row["deliveries"]),
                   n(row["distinct_events_delivered"]), n(row["rows_written"]),
                   n(row["duplicate_rows"]),
                   n(row["events_with_disagreeing_decisions"])))
    cti_s = [r for r in e4["spread"]["rows"]
             if r["guard"] == "check_then_insert"][0]
    cti_b = [r for r in e4["burst"]["rows"]
             if r["guard"] == "check_then_insert"][0]
    none_b = [r for r in e4["burst"]["rows"] if r["guard"] == "none"][0]
    add("exp4 spread size", "%s deliveries at concurrency %d"
        % (n(e4["spread"]["requests"]), e4["spread"]["concurrency"]))
    add("exp4 burst size", "%d events delivered %d times each"
        % (e4["burst"]["events"], e4["burst"]["copies"]))
    add("exp4 burst delay", "a %d ms delay against a %d ms budget"
        % (e4["burst"]["injected_db_delay_ms"], e4["burst"]["timeout_ms"]))
    add("cti duplicates", "writes %s duplicate rows under the spread "
        "workload and %s under the burst"
        % (n(cti_s["duplicate_rows"]), n(cti_b["duplicate_rows"])))
    add("removal share", "removes %s percent of the duplicates"
        % pct(e4["duplicates_removed_by_check_then_insert_under_burst"]))
    add("removal share in the summary", "removes %s percent of duplicate"
        % pct(e4["duplicates_removed_by_check_then_insert_under_burst"], 1))
    add("contradictory count", "%s of the %s events ended up with two"
        % (n(none_b["events_with_disagreeing_decisions"]),
           n(none_b["distinct_events_delivered"])))

    return want


def n_or_f(v):
    """The priced table writes whole costs without a decimal and fractional
    ones with two, because that is what json.dump produced from the rounding.
    Matching the README means reproducing that distinction rather than
    normalizing it away."""
    return n(v) if float(v) == int(v) else "{:,}".format(v)


def main():
    with open(README, encoding="utf-8") as fh:
        readme = fh.read()
    # Normalized on both sides so a reflowed paragraph is not a false alarm.
    # A checker that cries wolf on line wrapping is one a reader learns to
    # ignore, which is worse than not having one.
    flat = re.sub(r"\s+", " ", readme)

    want = build()
    missing = [(lbl, s) for lbl, s in want
               if re.sub(r"\s+", " ", s) not in flat]

    print("%d figures re-derived from results/*.json and checked against "
          "README.md" % len(want))
    if missing:
        print()
        for lbl, s in missing:
            print("MISSING (%s):" % lbl)
            print("    %s" % s)
        print()
        print("%d of %d figures are not in README.md verbatim."
              % (len(missing), len(want)))
        return 1
    print("all present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
