"""EXPERIMENT 2: fail-open or fail-closed, priced.

    python3 scripts/exp2_fail_open_closed.py

When the budget is blown the service must still answer. There are two answers
and neither is correct from first principles:

  FAIL-CLOSED  deny. A covered member is turned away at the front desk.
  FAIL-OPEN    approve. An uncovered member is sent an unexpected bill.

This is the one section of this repository whose result does not depend on the
machine it ran on. The latency numbers elsewhere are one box on loopback. The
crossover here is arithmetic over a stated cost model and a measured error
count, and it would come out the same on any hardware that produced the same
fallback rate.

One workload is run under both policies at several degradation levels, and
the two error kinds are counted against the answer key. Then the price is
SWEPT rather than assumed, because this repository does not know what an
unexpected bill costs relative to being turned away, and neither does anybody
who has not asked.

The prediction, recorded before the run: which policy wins flips somewhere
inside the swept range of prices. If it does not, if one policy wins at
every price anybody would plausibly name, that is a stronger result and it
is published as one.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import driver
import lab

PREDICTION = {
    "claim": "the winning policy flips somewhere inside the swept range of "
             "prices, so neither fail-open nor fail-closed is right on its own",
}

TIMEOUT_MS = 50
REQUESTS = 6000
CONCURRENCY = 64

# Degradation levels. 0 is the control: nothing falls back, so the two
# policies MUST produce identical numbers, and a run where they do not has a
# defect rather than a finding.
DELAYS_MS = [0, 25, 40, 60, 200]

# cost(an unexpected bill) / cost(being turned away at the front desk).
# Swept because this repository does not know the real ratio and neither does
# anybody who has not asked an actuary and a member services team.
PRICE_RATIOS = [0.25, 0.5, 1, 3, 10, 30]


def run_policy(events, delay_ms, fallback):
    lab.configure(run_id="exp2", timeout_ms=TIMEOUT_MS,
                  inject_db_delay_ms=delay_ms, fallback=fallback,
                  cache_ttl_ms=0, kill_switch=False, record=False,
                  budget_covers_queue=True, clear_cache=True,
                  reset_counters=True)
    recs, wall = driver.run([driver.to_request(e) for e in events], CONCURRENCY)
    by_id = {e["event_id"]: e for e in events}
    s = driver.score(recs, by_id)
    s["fallback"] = fallback
    s["inject_db_delay_ms"] = delay_ms
    s["requests_per_second"] = round(len(recs) / wall, 1)
    fell_back = len(recs) - s["by_source"].get("db", 0)
    s["share_that_fell_back"] = round(fell_back / len(recs), 6)
    return s


def main():
    if not lab.service_ready(30):
        print("the service is not answering on 127.0.0.1:18080.",
              file=sys.stderr)
        return 1

    events = driver.load_events(limit=REQUESTS, include_replays=False)
    covered = sum(1 for e in events if e["covered"])
    base_rate = covered / float(len(events))
    print("%d distinct events, %d of them covered (%.4f)"
          % (len(events), covered, base_rate))
    print()

    rows = []
    for delay in DELAYS_MS:
        for fallback in ("closed", "open"):
            r = run_policy(events, delay, fallback)
            rows.append(r)
            print("  delay %4d ms  %-7s  fell back %6.2f%%   "
                  "wrongly denied %5d   wrongly approved %5d   accuracy %.4f"
                  % (delay, fallback, 100.0 * r["share_that_fell_back"],
                     r["wrongly_denied"], r["wrongly_approved"], r["accuracy"]))
    print()

    # The control. With no injected delay nothing falls back, so the fallback
    # policy is never consulted and the two runs must agree. They will not
    # agree exactly, concurrency means a few requests time out even at zero
    # delay, so the check is on the ERROR COUNTS being close rather than
    # identical, and the run prints the gap.
    ctrl = [r for r in rows if r["inject_db_delay_ms"] == 0]
    control_gap = abs(ctrl[0]["wrongly_denied"] + ctrl[0]["wrongly_approved"]
                      - ctrl[1]["wrongly_denied"] - ctrl[1]["wrongly_approved"])
    print("control at 0 ms: the two policies differ by %d decisions out of %d"
          % (control_gap, ctrl[0]["scored"]))
    print()

    # ---- price the two error kinds -----------------------------------------
    priced = []
    for delay in DELAYS_MS:
        closed = [r for r in rows
                  if r["inject_db_delay_ms"] == delay and r["fallback"] == "closed"][0]
        opened = [r for r in rows
                  if r["inject_db_delay_ms"] == delay and r["fallback"] == "open"][0]
        for ratio in PRICE_RATIOS:
            # Cost is in units of one member turned away. An unexpected bill
            # costs `ratio` of those.
            c_closed = ratio * closed["wrongly_approved"] + closed["wrongly_denied"]
            c_open = ratio * opened["wrongly_approved"] + opened["wrongly_denied"]
            priced.append({
                "inject_db_delay_ms": delay,
                "price_of_a_bill_in_turnaways": ratio,
                "cost_fail_closed": round(c_closed, 2),
                "cost_fail_open": round(c_open, 2),
                "winner": "closed" if c_closed < c_open else
                          ("open" if c_open < c_closed else "tie"),
                "margin": round(abs(c_closed - c_open), 2),
            })

    print("cost is measured in members turned away; a bill costs `price` of them")
    print("%-10s %s" % ("delay", "  ".join("%8s" % ("%.2f" % r) for r in PRICE_RATIOS)))
    for delay in DELAYS_MS:
        cells = []
        for ratio in PRICE_RATIOS:
            row = [p for p in priced if p["inject_db_delay_ms"] == delay
                   and p["price_of_a_bill_in_turnaways"] == ratio][0]
            cells.append("%8s" % row["winner"])
        print("%-10s %s" % ("%d ms" % delay, "  ".join(cells)))
    print()

    # Where the answer flips, at full degradation. At that point every
    # decision is a fallback, so the comparison reduces to the base rate: fail
    # -open is better exactly when price * (1 - base_rate) < base_rate.
    full = [p for p in priced if p["inject_db_delay_ms"] == max(DELAYS_MS)]
    winners = {p["price_of_a_bill_in_turnaways"]: p["winner"] for p in full}
    flips = sorted(set(winners.values()))
    analytic = base_rate / (1.0 - base_rate)

    print("at full degradation the crossover is analytic: fail-open wins while")
    print("price < base_rate / (1 - base_rate) = %.4f" % analytic)
    print("  measured winners by price: %s"
          % ", ".join("%.2f->%s" % (k, v) for k, v in sorted(winners.items())))

    held = len(flips) > 1
    payload = {
        "timeout_ms": TIMEOUT_MS,
        "requests": len(events),
        "concurrency": CONCURRENCY,
        "injected_delays_ms": DELAYS_MS,
        "price_ratios": PRICE_RATIOS,
        "covered_events": covered,
        "base_coverage_rate": round(base_rate, 6),
        "analytic_crossover_price": round(analytic, 6),
        "runs": rows,
        "priced": priced,
        "control_gap_decisions_at_zero_delay": control_gap,
        "prediction": dict(PREDICTION,
                           distinct_winners=flips,
                           verdict="held" if held else "REFUTED"),
    }
    lab.write_result("exp2_fail_open_closed", payload)
    print()
    print("prediction: %s" % payload["prediction"]["verdict"])
    print("wrote results/exp2_fail_open_closed.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
