"""EXPERIMENT 4: is a replayed webhook genuinely idempotent?

    python3 scripts/exp4_idempotency.py

A payment processor retries whatever it did not get an acknowledgement for.
The same authorization arrives twice, or twenty times, or twenty times at the
same instant. Three implementations:

  none                no guard at all, as a baseline
  check_then_insert   look for an existing decision, and write one if there
                      is none. The implementation everybody writes first.
  unique              the database refuses the second row, and the caller is
                      handed the decision that already stands.

This is the one experiment in this repository whose result is a correctness
property rather than a timing one. Whether two concurrent deliveries can both
pass a check does not depend on how fast this machine is.

Two workloads, and the contrast between them is the finding:

  SPREAD    the natural arrival stream, where a replay lands thousands of
            events after its original.
  BURST     the same event delivered many times at once, which is what a
            retry storm actually looks like: the processor retries because
            it did not hear back, and it did not hear back because the service
            was slow, which means the retries arrive while it is still slow.

A delay is injected during the burst so that some deliveries answer from the
datastore and others fall back. That is what makes a duplicate DISAGREE with
its original rather than merely repeat it, and a decision log holding two
contradictory answers for one authorization is the failure worth naming.

The prediction, recorded before the run: check-then-insert is idempotent.
Expect it REFUTED under the burst and NOT under the spread, because the defect
is a race and a race needs concurrency to show.

One part of that expectation was wrong and the measurement says so. The
duplicates check-then-insert lets through do NOT disagree with each other, and
that is structural rather than luck: the deliveries that slip past the check
are the ones that arrive in the same instant, before any row exists, so they
are all doing identical work against an identically slow datastore and all
reach the same answer. The disagreeing duplicates appear where there is no
guard at all, because those deliveries are spread across the whole burst and
some of them fall back while others do not.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import driver
import lab

PREDICTION = {
    "claim": "check-then-insert makes the authorize endpoint idempotent",
}

GUARDS = ["none", "check_then_insert", "unique"]

SPREAD_REQUESTS = 8000
SPREAD_CONCURRENCY = 32

# The burst. BURST_EVENTS distinct authorizations, each delivered
# BURST_COPIES times, all in flight together.
BURST_EVENTS = 150
BURST_COPIES = 20
BURST_CONCURRENCY = 64

# Injected delay against a 50 ms budget, chosen so that a substantial share of
# deliveries fall back and the rest do not. A burst in which every delivery
# reached the datastore would produce duplicates that all agree, and the
# dangerous case would not appear.
BURST_TIMEOUT_MS = 50
BURST_DELAY_MS = 45


def table_for(guard):
    return "coverage.decision_unique" if guard == "unique" \
        else "coverage.decision"


def reset(run_id):
    lab.psql("DELETE FROM coverage.decision WHERE run_id = '%s';"
             " DELETE FROM coverage.decision_unique WHERE run_id = '%s';"
             % (run_id, run_id))


def audit(run_id, guard):
    """What the decision log actually holds after the run."""
    t = table_for(guard)
    row = lab.query_json("""
        SELECT count(*)                              AS rows_written,
               count(DISTINCT event_id)              AS distinct_events
          FROM %s WHERE run_id = '%s' AND guard = '%s'
    """ % (t, run_id, guard))[0]
    # An event with two different answers recorded. Not merely a duplicate: a
    # decision log that contains both "approved" and "denied" for one
    # authorization cannot be reconciled by anybody later, and no count of
    # duplicates alone says whether that happened.
    disagreeing = lab.query_json("""
        SELECT count(*) AS n FROM (
            SELECT event_id FROM %s
             WHERE run_id = '%s' AND guard = '%s'
             GROUP BY event_id
            HAVING count(DISTINCT approved) > 1) s
    """ % (t, run_id, guard))[0]["n"]
    row["duplicate_rows"] = row["rows_written"] - row["distinct_events"]
    row["events_with_disagreeing_decisions"] = disagreeing
    return row


def spread(guard):
    run_id = "exp4-spread-%s" % guard
    reset(run_id)
    lab.configure(run_id=run_id, guard=guard, record=True, timeout_ms=500,
                  inject_db_delay_ms=0, fallback="closed", cache_ttl_ms=0,
                  kill_switch=False, budget_covers_queue=True,
                  clear_cache=True, reset_counters=True)
    events = driver.load_events(limit=SPREAD_REQUESTS, include_replays=True)
    recs, wall = driver.run([driver.to_request(e) for e in events],
                            SPREAD_CONCURRENCY)
    a = audit(run_id, guard)
    a.update({
        "guard": guard,
        "workload": "spread",
        "deliveries": len(events),
        "replay_deliveries": sum(1 for e in events if e["is_replay"]),
        "distinct_events_delivered": len({e["event_id"] for e in events}),
        "responses_marked_replayed": sum(1 for r in recs
                                         if r.get("replayed")),
        "http_errors": sum(1 for r in recs if r.get("status") != 200),
        "requests_per_second": round(len(recs) / wall, 1),
    })
    return a


def burst(guard):
    run_id = "exp4-burst-%s" % guard
    reset(run_id)
    lab.configure(run_id=run_id, guard=guard, record=True,
                  timeout_ms=BURST_TIMEOUT_MS,
                  inject_db_delay_ms=BURST_DELAY_MS, fallback="closed",
                  cache_ttl_ms=0, kill_switch=False, budget_covers_queue=True,
                  clear_cache=True, reset_counters=True)
    events = driver.load_events(limit=BURST_EVENTS, include_replays=False)
    # Interleaved, not grouped. Sending twenty copies of one event and then
    # twenty of the next would let each group finish before the next began and
    # would quietly serialize the very thing under test.
    requests = []
    for _ in range(BURST_COPIES):
        for e in events:
            requests.append(driver.to_request(e))
    recs, wall = driver.run(requests, BURST_CONCURRENCY)
    a = audit(run_id, guard)
    by_source = {}
    for r in recs:
        if "source" in r:
            by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    a.update({
        "guard": guard,
        "workload": "burst",
        "deliveries": len(requests),
        "distinct_events_delivered": len(events),
        "copies_per_event": BURST_COPIES,
        "responses_marked_replayed": sum(1 for r in recs if r.get("replayed")),
        "http_errors": sum(1 for r in recs if r.get("status") != 200),
        "by_source": by_source,
        "requests_per_second": round(len(recs) / wall, 1),
    })
    return a


def main():
    if not lab.service_ready(30):
        print("the service is not answering on 127.0.0.1:18080.",
              file=sys.stderr)
        return 1

    print("SPREAD: the natural arrival stream, replays land far from their "
          "originals")
    spread_rows = []
    for guard in GUARDS:
        r = spread(guard)
        spread_rows.append(r)
        print("  %-18s deliveries %5d  distinct %5d  rows %5d  duplicates "
              "%4d  disagreeing %3d"
              % (guard, r["deliveries"], r["distinct_events_delivered"],
                 r["rows_written"], r["duplicate_rows"],
                 r["events_with_disagreeing_decisions"]))
    print()

    print("BURST: %d events x %d simultaneous copies, %d ms delay against a "
          "%d ms budget" % (BURST_EVENTS, BURST_COPIES, BURST_DELAY_MS,
                            BURST_TIMEOUT_MS))
    burst_rows = []
    for guard in GUARDS:
        r = burst(guard)
        burst_rows.append(r)
        print("  %-18s deliveries %5d  distinct %5d  rows %5d  duplicates "
              "%4d  DISAGREEING %3d"
              % (guard, r["deliveries"], r["distinct_events_delivered"],
                 r["rows_written"], r["duplicate_rows"],
                 r["events_with_disagreeing_decisions"]))
    print()

    def find(rows, guard):
        return [r for r in rows if r["guard"] == guard][0]

    cti_spread = find(spread_rows, "check_then_insert")
    cti_burst = find(burst_rows, "check_then_insert")
    uni_burst = find(burst_rows, "unique")
    none_burst = find(burst_rows, "none")

    looks_fine_when_spread = cti_spread["duplicate_rows"] == 0
    fails_under_burst = cti_burst["duplicate_rows"] > 0
    unique_holds = uni_burst["duplicate_rows"] == 0

    print("check-then-insert wrote %d duplicate rows under the spread workload"
          % cti_spread["duplicate_rows"])
    print("check-then-insert wrote %d duplicate rows under the burst, %d of "
          "them for events that ended up with two DIFFERENT answers on record"
          % (cti_burst["duplicate_rows"],
             cti_burst["events_with_disagreeing_decisions"]))
    print("the unique constraint wrote %d duplicates under the same burst"
          % uni_burst["duplicate_rows"])
    print("no guard at all wrote %d, and left %d of %d events with two "
          "CONTRADICTORY decisions on record"
          % (none_burst["duplicate_rows"],
             none_burst["events_with_disagreeing_decisions"],
             none_burst["distinct_events_delivered"]))
    print()
    print("THE SHAPE OF THE RESULT: check-then-insert removes %.2f%% of the "
          "duplicates" % (100.0 * (1 - cti_burst["duplicate_rows"]
                                   / float(none_burst["duplicate_rows"]))))
    print("without removing all of them, which is the worst kind of "
          "guarantee: it")
    print("survives every test anybody writes by hand and fails at volume.")

    payload = {
        "guards": GUARDS,
        "spread": {"requests": SPREAD_REQUESTS,
                   "concurrency": SPREAD_CONCURRENCY, "rows": spread_rows},
        "burst": {"events": BURST_EVENTS, "copies": BURST_COPIES,
                  "concurrency": BURST_CONCURRENCY,
                  "timeout_ms": BURST_TIMEOUT_MS,
                  "injected_db_delay_ms": BURST_DELAY_MS,
                  "rows": burst_rows},
        "check_then_insert_looks_correct_when_replays_are_spread":
            looks_fine_when_spread,
        "duplicates_removed_by_check_then_insert_under_burst": round(
            1 - cti_burst["duplicate_rows"]
            / float(none_burst["duplicate_rows"]), 6),
        # Why the surviving duplicates agree. The deliveries that slip past
        # the check are the ones racing in the first instant, before any row
        # exists; they are doing identical work against an identically slow
        # datastore and reach the same answer. Contradictory pairs need
        # deliveries spread across the burst, which is what the unguarded run
        # has.
        "why_check_then_insert_duplicates_agree":
            "the racers slip through in the same instant and do identical work",
        "check_then_insert_fails_under_simultaneous_replay": fails_under_burst,
        "unique_constraint_holds_under_simultaneous_replay": unique_holds,
        "prediction": dict(
            PREDICTION,
            duplicates_under_spread=cti_spread["duplicate_rows"],
            duplicates_under_burst=cti_burst["duplicate_rows"],
            disagreeing_under_burst=cti_burst["events_with_disagreeing_decisions"],
            verdict="REFUTED" if fails_under_burst else "held"),
    }
    lab.write_result("exp4_idempotency", payload)
    print()
    print("prediction: %s" % payload["prediction"]["verdict"])
    print("wrote results/exp4_idempotency.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
