"""EXPERIMENT 1: what the degradation actually looks like, and whether a
timeout bounds it.

    python3 scripts/exp1_degradation.py

The question this does not ask is "what is the p99". A p99 from one machine
with the data one hop away and no network in between is worth almost nothing.
What it asks instead is the SHAPE: as the datastore slows past the budget,
where does the latency distribution go, what fraction of answers stop being
real answers, and does the number in the config actually bound anything.

The no-op floor is measured first and published. /healthz touches no
connection, no cache and no lock, so it is what this harness costs before any
decision logic runs. Every other figure here is only meaningful above it.

The comparison that is the point. Two arrangements of the same timeout:

  budget_covers_queue = True    the deadline starts when the request arrives,
                                and time spent waiting for a connection comes
                                out of the same budget
  budget_covers_queue = False   wait as long as it takes for a connection,
                                THEN give the query the full timeout

The second is what a statement timeout alone buys, and it is what most
services actually have. Below saturation the two are indistinguishable, so
this has to be measured under load rather than argued about.

The prediction, recorded before the run: a configured timeout bounds the
service's observed tail. Scored separately for each arrangement, and written
out whichever way each one goes.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import driver
import lab

PREDICTION = {
    "claim": "a configured timeout bounds the service's observed tail, so the "
             "p99 stays within the budget plus the harness floor",
    "note": "scored for BOTH arrangements, and the headline is the ratio "
            "between them rather than either absolute verdict",
}

TIMEOUT_MS = 50
REQUESTS = 3000
CONCURRENCY = 64

# The pool holds 16 connections. Concurrency of 64 is deliberately above it:
# the two arrangements produce identical numbers until requests have to wait
# for a connection, so an experiment run below saturation would report that
# the distinction does not matter.
POOL_SIZE = 16

# Injected datastore delay, in milliseconds. Chosen to straddle the budget:
# below it nothing should time out, above it everything should.
DELAYS_MS = [0, 10, 25, 40, 60, 100, 200]


def sweep(events, budget_covers_queue):
    rows = []
    for delay in DELAYS_MS:
        lab.configure(run_id="exp1", timeout_ms=TIMEOUT_MS,
                      inject_db_delay_ms=delay, fallback="closed",
                      cache_ttl_ms=0, kill_switch=False, record=False,
                      budget_covers_queue=budget_covers_queue,
                      clear_cache=True, reset_counters=True)
        recs, wall = driver.run([driver.to_request(e) for e in events],
                                CONCURRENCY)
        lat = [r["latency_us"] for r in recs if r["latency_us"] is not None]
        by_source = {}
        for r in recs:
            if "source" in r:
                by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        errors = sum(1 for r in recs if r.get("status") != 200)
        driver_errors = sum(1 for r in recs if r.get("driver_error"))
        s = driver.summarize(lat)
        real = by_source.get("db", 0)
        row = dict(s)
        row.update({
            "inject_db_delay_ms": delay,
            "requests_per_second": round(len(recs) / wall, 1),
            "by_source": by_source,
            "share_answered_from_the_datastore": round(real / len(recs), 6),
            "share_that_fell_back": round(
                (len(recs) - real) / len(recs), 6),
            "http_errors": errors,
            "driver_errors": driver_errors,
        })
        rows.append(row)
        print("  delay %4d ms  p50 %7d  p95 %8d  p99 %8d  max %8d us   "
              "real answers %6.2f%%  %5.0f rps"
              % (delay, s["p50_us"], s["p95_us"], s["p99_us"], s["max_us"],
                 100.0 * row["share_answered_from_the_datastore"],
                 row["requests_per_second"]))
    return rows


def verdict(rows, floor_p99_us):
    """Did the timeout bound the tail, at every delay?

    The bound is the budget plus the measured floor, and it is a LOOSE
    reference rather than a law. The 99th percentile of a sum is not the sum
    of the 99th percentiles: a request that is unlucky in the scheduler and
    unlucky in the queue lands beyond both, so a correct implementation can
    exceed this and does. What means something is HOW FAR each arrangement
    exceeds it, and the two differ by an order of magnitude.

    An absolute verdict is deliberately not the headline. It sits close to
    the boundary for the sound arrangement and moves between runs, so a
    repository that led with it would be publishing a coin flip. The ratio
    between the two arrangements does not move.
    """
    bound_us = TIMEOUT_MS * 1000 + floor_p99_us
    worst = max(r["p99_us"] for r in rows)
    return {
        "bound_us": bound_us,
        "worst_p99_us": worst,
        "worst_p99_over_bound": round(worst / float(bound_us), 3),
        "within_bound": worst <= bound_us,
    }


def main():
    if not lab.service_ready(30):
        print("the service is not answering on 127.0.0.1:18080. Run:\n"
              "    cd stack && docker compose up -d", file=sys.stderr)
        return 1

    n = int(lab.scalar("SELECT count(*) FROM truth.arrival;"))
    if n == 0:
        print("no traffic loaded. Run: python3 scripts/load.py",
              file=sys.stderr)
        return 1

    events = driver.load_events(limit=REQUESTS, include_replays=False)
    print("%d requests, concurrency %d, pool %d, budget %d ms"
          % (len(events), CONCURRENCY, POOL_SIZE, TIMEOUT_MS))
    print()

    print("no-op floor (GET /healthz, touches nothing)")
    floor = driver.noop_floor(REQUESTS, CONCURRENCY)
    print("  p50 %d us   p95 %d us   p99 %d us   max %d us   %.0f rps"
          % (floor["p50_us"], floor["p95_us"], floor["p99_us"],
             floor["max_us"], floor["requests_per_second"]))
    print()

    print("budget covers the queue")
    covered = sweep(events, True)
    print()
    print("budget covers only the query (a statement timeout alone)")
    naive = sweep(events, False)
    print()

    v_covered = verdict(covered, floor["p99_us"])
    v_naive = verdict(naive, floor["p99_us"])

    ratio = v_naive["worst_p99_us"] / float(v_covered["worst_p99_us"])
    print("reference bound = budget %d ms + floor p99 %d us = %d us"
          % (TIMEOUT_MS, floor["p99_us"], v_covered["bound_us"]))
    print("  budget covers the queue : worst p99 %8d us  %.2fx the bound"
          % (v_covered["worst_p99_us"], v_covered["worst_p99_over_bound"]))
    print("  statement timeout alone : worst p99 %8d us  %.2fx the bound"
          % (v_naive["worst_p99_us"], v_naive["worst_p99_over_bound"]))
    print("  THE HEADLINE: the same 50 ms timeout produces a tail %.1f times"
          " longer" % ratio)
    print("  when it does not cover the queue.")

    payload = {
        "timeout_ms": TIMEOUT_MS,
        "requests_per_point": len(events),
        "concurrency": CONCURRENCY,
        "pool_size": POOL_SIZE,
        "injected_delays_ms": DELAYS_MS,
        "noop_floor": floor,
        "budget_covers_queue": covered,
        "statement_timeout_only": naive,
        "tail_ratio_naive_over_covered": round(ratio, 2),
        "prediction": dict(
            PREDICTION,
            with_budget_covering_the_queue=dict(
                v_covered,
                verdict="held" if v_covered["within_bound"] else "REFUTED"),
            with_a_statement_timeout_alone=dict(
                v_naive,
                verdict="held" if v_naive["within_bound"] else "REFUTED"),
            # Both exceed the reference bound, and the prediction is refuted
            # for both. The finding is the distance, not the verdict.
            verdict=("REFUTED"
                     if not (v_covered["within_bound"]
                             and v_naive["within_bound"]) else "held"),
        ),
    }
    lab.write_result("exp1_degradation", payload)
    print()
    print("wrote results/exp1_degradation.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
