"""Deterministically generate members, merchants, coverage changes and the
authorization events that arrive at the card terminal.

    python3 scripts/generate_traffic.py --summary

Every value is a pure function of a seed and a row number, so a clone
reproduces the same traffic and every number in README.md without fetching
anything. Nothing is drawn from a global random stream and nothing reads the
clock: a generator that reads the clock produces different data every run and
makes the whole repository unreproducible.

The timeline is in milliseconds from the start of the run, not in wall-clock
time. The experiments replay it as fast as they can and stamp their own
arrival times; what the generator fixes is the ORDER of events and status
changes relative to each other, which is the only part that has to be stable
for the answer key to mean anything.

What this is not. The arrival pattern, the coverage base rate and the rate at
which a provider's network status changes are all invented. Every count of
wrong decisions downstream is a function of them, and the README says so.
"""

import argparse
import hashlib
import json
import sys

SEED = "coverage-decision-service-2026"

N_MEMBERS = 5000
N_MERCHANTS = 2000
N_EVENTS = 20000

PLANS = ["PLAN-BRONZE", "PLAN-SILVER", "PLAN-GOLD"]

# Share of merchants that are health providers at all. The rest are ordinary
# businesses a member's card also touches, and they are covered by nobody.
# A decision service that only ever sees providers has never met its traffic.
PROVIDER_SHARE = 0.55

# Of the provider merchants, the share each plan covers at the start.
BASE_COVERAGE = 0.62

# Share of (plan, merchant) pairs whose coverage CHANGES during the run. This
# is the only reason a cache can be wrong, and experiment 3 is a measurement
# of it.
STATUS_CHANGE_SHARE = 0.06

# How concentrated the traffic is on a few merchants, and this constant is the
# difference between experiment 3 measuring something and measuring nothing.
# Real card traffic is heavily skewed: a small number of merchants take most
# of the volume. Uniform merchant selection would make a cache useless by
# construction. 20,000 events over 2,000 merchants is ten touches each, so
# nearly every lookup would be a miss and the TTL sweep would be a flat line.
# The skew is a log-uniform draw over the merchant ranks, which concentrates
# volume the way a Zipf distribution does without needing one.
MERCHANT_SKEW = True

# The run's timeline, in milliseconds. Events and status changes are spread
# across it deterministically.
RUN_MS = 600000

# Share of events that are a deliberate replay of a previous delivery. A payment
# processor retries whatever it did not get an acknowledgement for.
REPLAY_SHARE = 0.05


# ---------------------------------------------------------------------------
# THE DETERMINISTIC STREAM
# ---------------------------------------------------------------------------

def _bits(*parts):
    """64 bits keyed by SEED and by every part of the coordinate.

    Keyed rather than concatenated so that ("a", "bc") and ("ab", "c") cannot
    collide, which is the failure that makes a generator look random while
    quietly correlating two fields.
    """
    msg = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(
        hashlib.blake2b(msg, digest_size=8, key=SEED.encode()).digest(), "big")


def unit(*parts):
    return _bits(*parts) / 2.0 ** 64


def below(p, *parts):
    return unit(*parts) < p


def pick(seq, *parts):
    return seq[_bits(*parts) % len(seq)]


def skewed_rank(n, *parts):
    """A rank in [1, n], heavily concentrated on the low ranks.

    n ** u for u uniform in [0, 1) is log-uniform: half the draws land in the
    first sqrt(n) ranks. That is a coarse stand-in for the popularity
    distribution of real merchants, and nothing here is fitted to data.
    """
    return 1 + int(n ** unit(*parts)) % n


# ---------------------------------------------------------------------------
# THE STATIC WORLD
# ---------------------------------------------------------------------------

def members():
    return [{"member_id": i, "plan_id": pick(PLANS, "plan", i)}
            for i in range(1, N_MEMBERS + 1)]


def merchants():
    return [{"merchant_id": i,
             "is_provider": below(PROVIDER_SHARE, "isprov", i)}
            for i in range(1, N_MERCHANTS + 1)]


def network_status():
    """Every (plan, merchant) coverage row, including the ones that change.

    A non-provider merchant is never covered and gets no row at all. Absence
    is the answer for it, and the service has to treat a missing row as "not
    covered" rather than as an error, which is a decision, so it is stated
    here and tested.
    """
    rows = []
    for m in range(1, N_MERCHANTS + 1):
        if not below(PROVIDER_SHARE, "isprov", m):
            continue
        for plan in PLANS:
            covered = below(BASE_COVERAGE, "cov", plan, m)
            rows.append({"plan_id": plan, "merchant_id": m,
                         "covered": covered, "effective_from": 0})
            if below(STATUS_CHANGE_SHARE, "changes", plan, m):
                # The change lands in the middle 80 percent of the run, so
                # every change has traffic on both sides of it. A change in
                # the first or last instant would be unobservable and would
                # quietly dilute the staleness measurement.
                at = int(RUN_MS * 0.1) + _bits("changeat", plan, m) % int(
                    RUN_MS * 0.8)
                rows.append({"plan_id": plan, "merchant_id": m,
                             "covered": not covered, "effective_from": at})
    return rows


def coverage_at(status_index, plan_id, merchant_id, at_ms):
    """The correct answer for one (plan, merchant) at one instant.

    Status_index maps (plan_id, merchant_id) to its rows, sorted by
    effective_from. The latest row at or before at_ms WINS, and no row at all
    means not covered.
    """
    rows = status_index.get((plan_id, merchant_id))
    if not rows:
        return False
    answer = False
    for r in rows:
        if r["effective_from"] <= at_ms:
            answer = r["covered"]
        else:
            break
    return answer


# ---------------------------------------------------------------------------
# THE TRAFFIC
# ---------------------------------------------------------------------------

def arrivals(status_index):
    """The authorization events in ARRIVAL ORDER, replays included.

    A replay is not a new event. It shares the original's event_id, so the
    stream holds more entries than there are distinct events and the two are
    kept apart everywhere: truth.event has one row per distinct event and
    truth.arrival has one row per delivery. Storing the stream in a
    primary-keyed event table would be impossible, and storing only the
    distinct events would delete the entire subject of experiment 4.

    A replay carries the original's event_id and the original's timestamp.
    That is what a processor retry is: the same event, sent again. Giving the
    replay a fresh timestamp would make it a different authorization and would
    turn experiment 4 into a measurement of nothing.
    """
    out = []
    originals = []
    for i in range(1, N_EVENTS + 1):
        if originals and below(REPLAY_SHARE, "isreplay", i):
            src = originals[_bits("replaypick", i) % len(originals)]
            out.append(dict(src, seq=i, is_replay=True,
                            replay_of=src["event_id"]))
            continue

        member_id = 1 + _bits("member", i) % N_MEMBERS
        merchant_id = (skewed_rank(N_MERCHANTS, "merchant", i) if MERCHANT_SKEW
                       else 1 + _bits("merchant", i) % N_MERCHANTS)
        plan_id = pick(PLANS, "plan", member_id)
        # The timeline is monotonic with arrival order, and it has to be.
        # Experiment 3 applies the coverage changes progressively as the
        # stream is replayed, and the answer key says what was true at the
        # event's own timestamp. If timestamps were scattered, an event
        # arriving late in the stream could carry an early timestamp, and its
        # correct answer would be one the database had already moved past:
        # scoring a correct service as wrong for reasons nothing could fix.
        #
        # A replay still carries its original's timestamp, which is earlier
        # than its position in the stream. That is not an exception to this,
        # it is what a retry is.
        at = int(i * RUN_MS / N_EVENTS)
        ev = {
            "event_id": "evt-%08d" % i,
            "seq": i,
            "member_id": member_id,
            "merchant_id": merchant_id,
            "plan_id": plan_id,
            "offered_at_ms": at,
            "covered": coverage_at(status_index, plan_id, merchant_id, at),
            "is_replay": False,
            "replay_of": None,
        }
        out.append(ev)
        originals.append(ev)
    return out


def build():
    mem = members()
    mer = merchants()
    status = network_status()
    index = {}
    for r in status:
        index.setdefault((r["plan_id"], r["merchant_id"]), []).append(r)
    for rows in index.values():
        rows.sort(key=lambda r: r["effective_from"])
    stream = arrivals(index)
    seen, distinct = set(), []
    for e in stream:
        if e["event_id"] not in seen:
            seen.add(e["event_id"])
            distinct.append(e)
    return mem, mer, status, stream, distinct


MEMBER_COLS = ["member_id", "plan_id"]
MERCHANT_COLS = ["merchant_id", "is_provider"]
STATUS_COLS = ["plan_id", "merchant_id", "covered", "effective_from"]
EVENT_COLS = ["event_id", "seq", "member_id", "merchant_id", "plan_id",
              "offered_at_ms", "covered"]


ARRIVAL_COLS = ["seq", "event_id", "is_replay", "replay_of"]


def manifest(mem, mer, status, stream, distinct):
    """A hash over everything generated, so a run can prove it used the same
    input as the run that produced the shipped results."""
    h = hashlib.sha256()
    for cols, recs in ((MEMBER_COLS, mem), (MERCHANT_COLS, mer),
                       (STATUS_COLS, status), (EVENT_COLS, distinct),
                       (ARRIVAL_COLS, stream)):
        for r in recs:
            h.update("|".join("" if r.get(c) is None else str(r[c])
                              for c in cols).encode("utf-8"))
            h.update(b"\n")
    replays = sum(1 for e in stream if e["is_replay"])
    covered = sum(1 for e in stream if e["covered"])
    changed = len([1 for r in status if r["effective_from"] > 0])
    # How concentrated the traffic actually came out. Published rather than
    # assumed: the skew is the reason the cache experiment has any signal, so
    # the number a reader needs is the realized concentration and not the
    # constant that produced it.
    per_merchant = {}
    for e in stream:
        per_merchant[e["merchant_id"]] = per_merchant.get(e["merchant_id"], 0) + 1
    ranked = sorted(per_merchant.values(), reverse=True)
    top10 = sum(ranked[:max(1, len(ranked) // 10)])
    return {
        "seed": SEED,
        "members": len(mem),
        "merchants": len(mer),
        "provider_merchants": sum(1 for m in mer if m["is_provider"]),
        "network_status_rows": len(status),
        "status_changes_during_the_run": changed,
        "arrivals": len(stream),
        "replay_arrivals": replays,
        "distinct_events": len(distinct),
        "merchants_touched": len(per_merchant),
        "share_of_traffic_on_the_top_10_percent_of_merchants":
            round(top10 / float(len(stream)), 6),
        "events_that_should_be_approved": covered,
        "base_approval_rate": round(covered / float(len(stream)), 6),
        "run_ms": RUN_MS,
        "sha256": h.hexdigest(),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--summary", action="store_true",
                    help="print the manifest and exit")
    a = ap.parse_args()
    mem, mer, status, stream, distinct = build()
    man = manifest(mem, mer, status, stream, distinct)
    print(json.dumps(man, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
