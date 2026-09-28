"""Re-derive the published figures from results/*.json and the generator, and
require them verbatim in README.md.

    python3 scripts/check_readme_numbers.py
    python3 scripts/check_readme_numbers.py --emit    print what it derives

Not derived: the figures in the claims table's "(mutation-checked: ...)"
notes, which describe an edit and what it produced (the no-op floor's 42
ms); the HTTP status codes 404 and 500; the minimum Python version, 3.11;
and the service's fixed constants quoted in prose. Every other number in
README.md is a row --emit prints.

Why this exists. A number in a document has no owner. The results files are
rewritten by every run; the prose is rewritten by hand, sometimes, when
somebody remembers. This script makes the prose fail instead of drift.

It covers sentences, not only table rows. A percentage inside a paragraph does
not look like a figure to a reader or to whoever writes a deriver, so it is
the one most likely to go stale.

It prints how many figures it checked whether or not any are missing, so a
version of this file that has silently stopped deriving half of them is visible
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
    # Rounded, not truncated: the scripts print throughput with %.0f, which
    # rounds, and the README has to agree with what they print.
    return "{:,}".format(int(round(x)))


def f(x, places):
    return "%.*f" % (places, float(x))


def pct(x, places=2):
    return "%.*f" % (places, float(x) * 100.0)


def nai_share_25(e1):
    """The statement-timeout-only arm's share answered from the datastore
    at 25 ms, the figure section 1 sets against the sound arrangement's."""
    (row,) = [r for r in e1["statement_timeout_only"]
              if r["inject_db_delay_ms"] == 25]
    return row["share_answered_from_the_datastore"]


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
    # Every sweep point the results file holds, not a tuple written out by
    # hand: a subset iterated by a deriver certifies the rows it renders and
    # says nothing about the one it skips, and the 100 ms row carries the
    # worst p99 the prose quotes.
    for d in sorted(e1["injected_delays_ms"]):
        add("degradation row %d" % d, "| %d ms | %s us | %s us | %s us | %s us |"
            % (d, n(cov[d]["p50_us"]), n(cov[d]["p99_us"]),
               n(nai[d]["p50_us"]), n(nai[d]["p99_us"])))

    p = e1["prediction"]
    add("worst p99 both ways",
        "The worst p99 is %s us when the budget covers the queue and %s us when"
        % (n(p["with_budget_covering_the_queue"]["worst_p99_us"]),
           n(p["with_a_statement_timeout_alone"]["worst_p99_us"])))
    # Each row anchored to its own sentence. "a tail 10.8 times longer" is a
    # substring of BOTH copies, so one intact copy used to satisfy both rows.
    add("tail ratio in section 1", "the same configured timeout, a tail %s "
        "times longer" % f(e1["tail_ratio_naive_over_covered"], 1))
    add("tail ratio in the summary", "The same %d ms timeout produces a tail "
        "%s times longer" % (e1["timeout_ms"],
                             f(e1["tail_ratio_naive_over_covered"], 1)))
    add("timeout respected", "respected %d ms in both runs" % e1["timeout_ms"])
    add("timeout in the arm list", "give the query the full %d ms"
        % e1["timeout_ms"])
    add("naive answers at 25 ms", "while the naive one answers %s percent"
        % f(100 * nai_share_25(e1), 0))
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
    add("changes held back in exp1 and exp2", "applying the %d changes as they "
        "fall due" % e3["coverage_changes"])
    add("exp1/exp2 event count", "the first %s events have a key"
        % n(e2["requests"]))
    add("exp3 replay length", "(The experiment replays the first %s"
        % n(e3["requests"]))
    add("exp3 denominator", "it is not %s. Only the %d decisions whose"
        % (n(e3["requests"]), e3["events_affected_by_a_change"]))
    add("exp3 table header", "| Wrong among the %d affected |"
        % e3["events_affected_by_a_change"])
    add("exp3 no-datastore run", "The same %s events, answered without "
        "touching the datastore" % n(e3["requests"]))
    add("exp3 delay in Limits", "**THE %d ms datastore delay in section 3"
        % e3["injected_db_delay_ms"])
    # Derive from what was applied, not from the world total. The world holds
    # 194 changes; the replayed window applies 130, and every ttl_sweep row
    # records that. Deriving the sentence from the total certified a claim the
    # results file contradicts.
    applied = sorted({r["changes_applied"] for r in e3["ttl_sweep"]})
    if len(applied) != 1:
        # Raised and not asserted: `python3 -O` deletes an assert statement
        # and the message with it, and this one is the difference between
        # certifying a sentence and certifying a sentence built from rows
        # that disagree with each other.
        raise ValueError("ttl sweep rows disagree on changes_applied: %s"
                         % (applied,))
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
    # The approvals a fail-closed fallback cannot have made. It can only
    # deny, so every one of these came from the datastore, and the README
    # paragraph that says so must carry the measured count rather than a
    # remembered one.
    ctrl_closed = [r for r in e2["runs"] if r["inject_db_delay_ms"] == 0
                   and r["fallback"] == "closed"][0]
    add("approvals the closed fallback cannot explain",
        "the closed run records %s wrongly approved"
        % n(ctrl_closed["wrongly_approved"]))
    runs = {(r["inject_db_delay_ms"], r["fallback"]): r for r in e2["runs"]}
    for d in sorted(e2["injected_delays_ms"]):        # every row, not a subset
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
    # The multiple between the cheapest and dearest step is not derived. It is
    # a ratio against a denominator near zero, it moved by a factor of two
    # between two runs of the same experiment, and publishing it would be
    # publishing noise. The dearest step's own value is stable and is what the
    # README quotes.
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

    # ---- the summary, the world, and prose the tables above do not hold ----
    add("free ttl in the summary", "%d ms cache TTL buys a %s percent hit rate"
        % (e3["largest_free_ttl_ms"],
           pct(e3["hit_rate_at_the_largest_free_ttl"], 1)))
    longest = [r for r in e3["ttl_sweep"]
               if r["cache_ttl_ms"] == e3["hit_rate_saturates_at_ttl_ms"]][0]
    add("longest ttl in the summary",
        "a %d second TTL gets %d percent of the changed-coverage decisions"
        % (longest["cache_ttl_ms"] // 1000,
           round(100 * longest["affected_error_rate"])))
    applied = longest["changes_applied"]

    import generate_traffic as gen
    mem, mer, status, stream, distinct = gen.build()
    man = gen.manifest(mem, mer, status, stream, distinct)
    add("world in section 0", "since %s events over %s merchants is %s touches "
        "each" % (n(gen.N_EVENTS), n(gen.N_MERCHANTS),
                  lab.NUMBER_WORDS[gen.N_EVENTS // gen.N_MERCHANTS]))
    if man["sha256"] != e1["input_manifest_sha256"]:
        raise SystemExit("the generator no longer produces the world the "
                         "results were measured on, so the world table cannot "
                         "be derived from it")
    add("world members", "| members, on three plans | %s |" % n(man["members"]))
    add("world merchants", "| merchants | %s, of which %s are health providers |"
        % (n(man["merchants"]), n(man["provider_merchants"])))
    add("world rows", "| coverage rows at the start | %s |"
        % n(man["network_status_rows"] - man["status_changes_during_the_run"]))
    add("world changes", "| coverage changes during the run | %s |"
        % n(man["status_changes_during_the_run"]))
    add("world arrivals", "| authorization deliveries | %s |" % n(man["arrivals"]))
    add("world distinct", "| distinct events among them | %s |"
        % n(man["distinct_events"]))
    add("world replays", "| deliveries that are a processor retry | %s |"
        % n(man["replay_arrivals"]))
    add("world approved", "| share that should be approved | %s percent |"
        % pct(man["base_approval_rate"], 1))
    add("world skew", "the busiest 10 percent of the %s merchants that got "
        "traffic take %s percent of the volume"
        % (n(man["merchants_touched"]),
           pct(man["share_of_traffic_on_the_top_10_percent_of_merchants"], 1)))

    events = {e["event_id"]: e for e in distinct}
    window = [a for a in stream if not a["is_replay"]][:e3["requests"]]
    end_ms = max(events[a["event_id"]]["offered_at_ms"] for a in window)
    due = sum(1 for r in status if 0 < r["effective_from"] <= end_ms)
    add("exp3 window", "ending at %s ms of a %s ms timeline; %d changes are "
        "due by then and the 40-chunk application schedule applies %d"
        % (n(end_ms), n(man["run_ms"]), due, applied))
    add("exp3 applied in prose", "%d coverage changes and %d of them fall inside"
        % (man["status_changes_during_the_run"], applied))

    runs = {(r["inject_db_delay_ms"], r["fallback"]): r for r in e2["runs"]}
    fb = [sum(v for k, v in runs[(0, pol)]["by_source"].items()
              if k.startswith("fallback")) for pol in ("closed", "open")]
    add("control fallbacks", "fires on %d and %d of the %s requests (%s%% and "
        "%s%%)" % (fb[0], fb[1], n(e2["requests"]),
                   f(100.0 * fb[0] / e2["requests"], 1),
                   f(100.0 * fb[1] / e2["requests"], 1)))
    totals = [runs[(0, pol)]["wrongly_denied"] + runs[(0, pol)]["wrongly_approved"]
              for pol in ("closed", "open")]
    add("disagreement bounds", "at least %d and at most %d"
        % (abs(totals[0] - totals[1]), totals[0] + totals[1]))
    crossings = []
    for d in sorted({k[0] for k in runs}):
        c, o = runs[(d, "closed")], runs[(d, "open")]
        crossings.append((d, (c["wrongly_denied"] - o["wrongly_denied"])
                          / float(o["wrongly_approved"] - c["wrongly_approved"])))
    add("crossover per delay", "it is %s at %d ms, %s at %d ms, %s at %d ms"
        % (f(crossings[0][1], 4), crossings[0][0], f(crossings[1][1], 4),
           crossings[1][0], f(crossings[2][1], 4), crossings[2][0]))
    # "at both 60 ms and 200 ms" is a claim that the last two are equal.
    if f(crossings[3][1], 4) != f(crossings[4][1], 4):
        raise ValueError("the crossover no longer settles: %r" % crossings[3:])
    add("crossover settled", "and %s at both %d ms and %d ms"
        % (f(crossings[3][1], 4), crossings[3][0], crossings[4][0]))
    at_half = {r["inject_db_delay_ms"]: r["winner"] for r in e2["priced"]
               if r["price_of_a_bill_in_turnaways"] == 0.5}
    add("price 0.50 winners", "At a price of 0.50 the winner is %s at 0 ms and "
        "%s at 25 ms" % (at_half[0], at_half[25]))
    add("guard failure rate", "fails %s percent of the time"
        % f(100 * (1 - e4["duplicates_removed_by_check_then_insert_under_burst"]), 1))
    cti_sources = cti_b["by_source"]
    add("exp4 cti fallbacks", "fell back on only %d of its %d decisions"
        % (cti_sources.get("fallback_closed", 0) + cti_sources.get("fallback_open", 0),
           cti_sources.get("db", 0) + cti_sources.get("fallback_closed", 0)
           + cti_sources.get("fallback_open", 0)))

    # ---- how many predictions were refuted ---------------------------------
    # Derived, because it is the figure a reader is least likely to check and
    # the one this repository's argument rests on: refuted predictions are
    # kept, so the count of them is a claim about honesty.
    verdicts = lab.scored_predictions(e1, e2, e3, e4)
    refuted = [label for label, v in verdicts if v == "REFUTED"]
    add("predictions refuted",
        "%s of the %s predictions here were refuted and all %s are still in "
        "the code"
        % (lab.NUMBER_WORDS[len(refuted)].capitalize(),
           lab.NUMBER_WORDS[len(verdicts)], lab.NUMBER_WORDS[len(refuted)]))

    return want


def n_or_f(v):
    """The priced table writes whole costs without a decimal and fractional
    ones with two, because that is what json.dump produced from the rounding.
    Matching the README means reproducing that distinction rather than
    normalizing it away."""
    return n(v) if float(v) == int(v) else "{:,}".format(v)


def main():
    if "--emit" in sys.argv:
        for label, text in build():
            print("%s\n%s" % (label, text))
        return 0
    with open(README, encoding="utf-8") as fh:
        readme = fh.read()
    try:
        return _check(readme)
    except lab.MissingResult as exc:
        # A checker that cannot look must say so, not die with a traceback
        # naming a line in lab.py.
        print(str(exc), file=sys.stderr)
        return 1


def _check(readme):
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
