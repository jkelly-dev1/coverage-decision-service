"""EXPERIMENT 3: what a cache buys, what it costs when it is wrong, and what
the kill switch actually does.

    python3 scripts/exp3_cache_and_kill_switch.py

A cache makes the service fast and makes it wrong for a while. Those are the
same setting. This sweeps the TTL and measures both sides against the answer
key at the moment of each decision.

The coverage changes are applied as the stream is replayed, not loaded up
front. 194 (plan, merchant) pairs change status partway through the run, and
the driver inserts each change when the virtual clock passes it. That is the
only reason a cached answer can be stale, and loading them all at the start
would make every staleness figure here a measurement of the loader.

The denominator is stated and it is not 20,000. Only the decisions whose
coverage had actually changed can be wrong because of staleness. Reporting
stale errors as a share of all decisions would divide a real effect by a
number chosen to make it look small; this reports them against the events that
were affected, and states that count.

The timeout is deliberately generous here. Experiment 1 is about the budget;
this one is about the cache, and a run where requests were also falling back
would confound the two. Nothing times out in this experiment and the results
file records that.

Two predictions, recorded before the run:
  A. Median latency is monotonically non-increasing as the TTL grows, so
     latency never argues for a shorter TTL.
  B. Every gain in hit rate is paid for with a proportional gain in wrong
     answers on the decisions whose coverage had changed, so there is no free
     region and the TTL is a straight trade.

The marginal trade is what decides a TTL, not the averages. What matters is
what the NEXT increment buys and what it costs, and the report gives it step
by step and not only as a curve.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import driver
import generate_traffic as gen
import lab

# Two predictions, both falsifiable, recorded before the run. The first is
# the assumption a cache is usually added under; the second is the one that
# decides what TTL to actually pick.
PREDICTIONS = {
    "longer_is_better_for_latency": {
        "claim": "median latency is monotonically non-increasing as the TTL "
                 "grows, so latency never argues for a shorter TTL",
    },
    "no_ttl_buys_hit_rate_for_free": {
        "claim": "every increase in hit rate is paid for with a proportional "
                 "increase in wrong answers on decisions whose coverage had "
                 "changed, so there is no free region",
    },
}

REQUESTS = 12000
CONCURRENCY = 16
TIMEOUT_MS = 500

# A datastore one network hop away. The Postgres in this compose file answers
# in about a millisecond over loopback, which is not what a decision service
# talks to and would make a cache look pointless. 20 ms is stated as a
# stand-in for a real round trip and is not measured from anything.
DB_DELAY_MS = 20

TTLS_MS = [0, 250, 1000, 5000, 30000, 300000]

CHUNKS = 40


def reset_status():
    """Put coverage.network_status back to what was true at time zero."""
    lab.psql("DELETE FROM coverage.network_status WHERE effective_from > 0;")


def pending_changes():
    _, _, status, _, _ = gen.build()
    return sorted((r for r in status if r["effective_from"] > 0),
                  key=lambda r: r["effective_from"])


def affected_event_ids(changes):
    """Events whose own (plan, merchant) had already changed when they arrived.

    The only events a stale cache can be wrong about. Everything else has the
    same answer before and after, so including them in the denominator would
    say more about the traffic mix than about the cache.
    """
    by_pair = {}
    for c in changes:
        by_pair.setdefault((c["plan_id"], c["merchant_id"]), []).append(
            c["effective_from"])
    ids = set()
    for e in driver.load_events(include_replays=False):
        for at in by_pair.get((e["plan_id"], e["merchant_id"]), []):
            if at <= e["offered_at_ms"]:
                ids.add(e["event_id"])
                break
    return ids


def run_ttl(events, changes, ttl_ms, affected):
    """Replay the stream in chunks, applying changes as the clock passes them."""
    reset_status()
    lab.configure(run_id="exp3", timeout_ms=TIMEOUT_MS,
                  inject_db_delay_ms=DB_DELAY_MS, fallback="closed",
                  cache_ttl_ms=ttl_ms, kill_switch=False, record=False,
                  budget_covers_queue=True, clear_cache=True,
                  reset_counters=True)

    size = max(1, len(events) // CHUNKS)
    applied = 0
    records = []
    for start in range(0, len(events), size):
        chunk = events[start:start + size]
        # Apply every change the virtual clock has reached, before the chunk
        # that is allowed to see it. Applying them after would let an event
        # be scored against a change the database had not been told about.
        virtual_ms = chunk[0]["offered_at_ms"]
        due = [c for c in changes[applied:] if c["effective_from"] <= virtual_ms]
        if due:
            lab.copy_rows("coverage.network_status", gen.STATUS_COLS, due)
            applied += len(due)
        recs, _ = driver.run([driver.to_request(e) for e in chunk], CONCURRENCY)
        records.extend(recs)

    by_id = {e["event_id"]: e for e in events}
    s = driver.score(records, by_id)
    lat = [r["latency_us"] for r in records if r["latency_us"] is not None]

    # Errors split by whether the decision could possibly have been a
    # staleness error at all.
    aff_total = aff_wrong = aff_wrong_from_cache = 0
    for r in records:
        if r.get("status") != 200 or "approved" not in r:
            continue
        if r["event_id"] not in affected:
            continue
        aff_total += 1
        truth = by_id.get(r["event_id"])
        if truth is not None and r["approved"] != truth["covered"]:
            aff_wrong += 1
            if r["source"] == "cache":
                aff_wrong_from_cache += 1

    stats = lab.get("/stats")
    row = dict(driver.summarize(lat))
    row.update({
        "cache_ttl_ms": ttl_ms,
        "changes_applied": applied,
        "accuracy": s["accuracy"],
        "wrongly_denied": s["wrongly_denied"],
        "wrongly_approved": s["wrongly_approved"],
        "by_source": s["by_source"],
        "cache_hit_share": round(
            s["by_source"].get("cache", 0) / float(s["scored"]), 6),
        "fell_back": s["by_source"].get("fallback_closed", 0)
        + s["by_source"].get("fallback_open", 0),
        "affected_decisions": aff_total,
        "affected_wrong": aff_wrong,
        "affected_wrong_served_from_cache": aff_wrong_from_cache,
        "affected_error_rate": round(aff_wrong / aff_total, 6) if aff_total else 0.0,
        "counters": stats["counters"],
    })
    return row


def main():
    if not lab.service_ready(30):
        print("the service is not answering on 127.0.0.1:18080.",
              file=sys.stderr)
        return 1

    changes = pending_changes()
    events = driver.load_events(limit=REQUESTS, include_replays=False)
    affected = affected_event_ids(changes)
    affected_here = sum(1 for e in events if e["event_id"] in affected)
    print("%d events, %d coverage changes, %d events affected by one"
          % (len(events), len(changes), affected_here))
    print("datastore delay %d ms, timeout %d ms, concurrency %d"
          % (DB_DELAY_MS, TIMEOUT_MS, CONCURRENCY))
    print()

    rows = []
    for ttl in TTLS_MS:
        r = run_ttl(events, changes, ttl, affected)
        rows.append(r)
        print("  ttl %7d ms  hits %6.2f%%  p50 %6d  p99 %7d us  "
              "accuracy %.4f  affected wrong %3d/%-3d (%.4f)"
              % (ttl, 100.0 * r["cache_hit_share"], r["p50_us"], r["p99_us"],
                 r["accuracy"], r["affected_wrong"], r["affected_decisions"],
                 r["affected_error_rate"]))
    print()

    no_cache = rows[0]
    best_lat = min(rows, key=lambda r: r["p50_us"])
    worst_err = max(rows, key=lambda r: r["affected_error_rate"])

    # The marginal trade, step by step. The averages hide the shape: what
    # decides a TTL is what the NEXT increment buys and what it costs, not
    # what the whole cache is worth.
    baseline_err = rows[0]["affected_error_rate"]
    best_hits = max(r["cache_hit_share"] for r in rows)
    saturates_at = None
    for r in rows:
        if r["cache_ttl_ms"] > 0 and r["cache_hit_share"] >= best_hits - 0.01:
            saturates_at = r["cache_ttl_ms"]
            break

    steps = []
    for a, b in zip(rows, rows[1:]):
        d_hit = b["cache_hit_share"] - a["cache_hit_share"]
        d_err = b["affected_error_rate"] - a["affected_error_rate"]
        steps.append({
            "from_ttl_ms": a["cache_ttl_ms"], "to_ttl_ms": b["cache_ttl_ms"],
            "hit_rate_gained": round(d_hit, 6),
            "affected_error_added": round(d_err, 6),
            "error_added_per_point_of_hit_rate":
                round(d_err / d_hit, 4) if d_hit > 1e-9 else None,
        })

    # A free region exists: the largest TTL whose error rate on affected
    # decisions is no worse than running with no cache at all. If one exists,
    # hit rate up to that point costs nothing, which is the opposite of the
    # second prediction.
    free_ttl = 0
    free_hits = 0.0
    for r in rows:
        if r["affected_error_rate"] <= baseline_err + 1e-9:
            free_ttl = r["cache_ttl_ms"]
            free_hits = r["cache_hit_share"]

    print("marginal trade, per step")
    for st in steps:
        print("  %7d -> %-7d ms   hit rate %+.4f   affected error %+.4f   %s"
              % (st["from_ttl_ms"], st["to_ttl_ms"], st["hit_rate_gained"],
                 st["affected_error_added"],
                 ("%.2f error per point of hit"
                  % st["error_added_per_point_of_hit_rate"])
                 if st["error_added_per_point_of_hit_rate"] is not None
                 else "no additional hit rate"))
    print()
    print("hit rate saturates at a TTL of %s ms" % saturates_at)
    print("A TTL of %d ms buys %.2f%% hit rate at NO additional error over "
          "running with no cache at all." % (free_ttl, 100.0 * free_hits))

    # ---- the kill switch ---------------------------------------------------
    print()
    print("kill switch")
    reset_status()
    lab.configure(run_id="exp3ks", timeout_ms=TIMEOUT_MS,
                  inject_db_delay_ms=DB_DELAY_MS, cache_ttl_ms=0,
                  kill_switch=False, record=False, clear_cache=True,
                  reset_counters=True)
    base_recs, _ = driver.run([driver.to_request(e) for e in events],
                              CONCURRENCY)
    base_by_id = {r["event_id"]: r for r in base_recs if "approved" in r}

    ks_rows = []
    for answer in (False, True):
        lab.configure(kill_switch=True, kill_switch_answer=answer,
                      clear_cache=True, reset_counters=True)
        recs, wall = driver.run([driver.to_request(e) for e in events],
                                CONCURRENCY)
        by_id = {e["event_id"]: e for e in events}
        s = driver.score(recs, by_id)
        lat = [r["latency_us"] for r in recs if r["latency_us"] is not None]
        changed = sum(1 for r in recs if "approved" in r
                      and r["event_id"] in base_by_id
                      and r["approved"] != base_by_id[r["event_id"]]["approved"])
        row = dict(driver.summarize(lat))
        row.update({
            "kill_switch_answer": answer,
            "decisions_changed_vs_normal": changed,
            "share_of_decisions_changed": round(changed / float(len(recs)), 6),
            "accuracy": s["accuracy"],
            "wrongly_denied": s["wrongly_denied"],
            "wrongly_approved": s["wrongly_approved"],
            "requests_per_second": round(len(recs) / wall, 1),
        })
        ks_rows.append(row)
        print("  answer=%-5s  p50 %5d us  %6.0f rps  changed %5d decisions "
              "(%.2f%%)  accuracy %.4f"
              % (answer, row["p50_us"], row["requests_per_second"], changed,
                 100.0 * row["share_of_decisions_changed"], row["accuracy"]))
    lab.configure(kill_switch=False)

    latencies = [r["p50_us"] for r in rows]
    monotone = all(b <= a for a, b in zip(latencies, latencies[1:]))
    free_exists = free_ttl > 0

    print()
    print("prediction A (longer TTL never hurts latency): %s"
          % ("held" if monotone else "REFUTED"))
    print("prediction B (no TTL buys hit rate for free): %s"
          % ("REFUTED" if free_exists else "held"))

    payload = {
        "requests": len(events),
        "concurrency": CONCURRENCY,
        "timeout_ms": TIMEOUT_MS,
        "injected_db_delay_ms": DB_DELAY_MS,
        "ttls_ms": TTLS_MS,
        "coverage_changes": len(changes),
        "events_affected_by_a_change": affected_here,
        "ttl_sweep": rows,
        "hit_rate_saturates_at_ttl_ms": saturates_at,
        "marginal_steps": steps,
        "no_cache_affected_error_rate": baseline_err,
        "largest_free_ttl_ms": free_ttl,
        "hit_rate_at_the_largest_free_ttl": round(free_hits, 6),
        "kill_switch": ks_rows,
        "predictions": {
            "longer_is_better_for_latency": dict(
                PREDICTIONS["longer_is_better_for_latency"],
                p50_by_ttl_us=latencies,
                best_at_ttl_ms=best_lat["cache_ttl_ms"],
                verdict="held" if monotone else "REFUTED"),
            "no_ttl_buys_hit_rate_for_free": dict(
                PREDICTIONS["no_ttl_buys_hit_rate_for_free"],
                largest_free_ttl_ms=free_ttl,
                hit_rate_at_that_ttl=round(free_hits, 6),
                worst_affected_error_rate=worst_err["affected_error_rate"],
                verdict="REFUTED" if free_exists else "held"),
        },
    }
    lab.write_result("exp3_cache_and_kill_switch", payload)
    print()
    print("wrote results/exp3_cache_and_kill_switch.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
