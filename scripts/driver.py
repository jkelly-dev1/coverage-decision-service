"""The load driver: replay authorizations against the service and record what
came back.

STANDARD LIBRARY ONLY, and CONNECTIONS ARE REUSED. Opening a TCP connection
per request would put the handshake inside every latency number, and at these
scales the handshake is a large share of the measurement. Each worker thread
holds one keep-alive connection for the whole run, which is also what a real
caller does.

What this measures and what it does not. It measures the time from the
driver's send to the driver's receive, on loopback, on one machine. That
includes Python's HTTP server, the interpreter, and the driver itself. The
no-op floor below exists so a reader can see how much of any number is this
harness rather than the decision path.
"""

import http.client
import json
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lab

HOST = "127.0.0.1"
PORT = 18080


def percentile(values, p):
    """The p-th percentile by nearest rank, on an already-sorted list.

    Nearest rank, not interpolated. An interpolated percentile invents a
    latency that no request actually experienced, which is fine for a smooth
    distribution and misleading for one with a hard timeout in it: a cluster
    of requests has to sit exactly at the budget.
    """
    if not values:
        return None
    k = max(0, min(len(values) - 1, int(round(p / 100.0 * len(values) + 0.5)) - 1))
    return values[k]


def summarize(latencies_us):
    s = sorted(latencies_us)
    return {
        "n": len(s),
        "min_us": s[0] if s else None,
        "p50_us": percentile(s, 50),
        "p95_us": percentile(s, 95),
        "p99_us": percentile(s, 99),
        "max_us": s[-1] if s else None,
        "mean_us": int(sum(s) / len(s)) if s else None,
    }


class Worker(threading.Thread):
    def __init__(self, work, out, path="/authorize", ready=None):
        super().__init__(daemon=True)
        self.work = work
        self.out = out
        self.path = path
        self.ready = ready
        self.conn = http.client.HTTPConnection(HOST, PORT, timeout=120)

    def _warm(self):
        """Establish this worker's connection BEFORE the clock starts.

        Connection setup is not what any experiment here measures. Opening 64
        sockets simultaneously is a burst the accept queue has to absorb, and
        whatever it costs belongs to the harness rather than to the decision
        path. Every worker connects and exchanges one throwaway request, and
        only then does the timed run begin.
        """
        self.conn.request("GET", "/healthz")
        self.conn.getresponse().read()

    def run(self):
        headers = {"Content-Type": "application/json"}
        if self.ready is not None:
            try:
                self._warm()
            finally:
                self.ready.wait()
        while True:
            item = self.work.get()
            if item is None:
                self.work.task_done()
                break
            try:
                body = json.dumps(item).encode("utf-8") if item != "GET" else None
                t0 = time.perf_counter()
                if body is None:
                    self.conn.request("GET", self.path)
                else:
                    self.conn.request("POST", self.path, body=body,
                                      headers=headers)
                resp = self.conn.getresponse()
                payload = resp.read()
                elapsed_us = int((time.perf_counter() - t0) * 1e6)
                rec = {"status": resp.status, "latency_us": elapsed_us}
                if resp.status == 200 and body is not None:
                    rec.update(json.loads(payload.decode("utf-8")))
                self.out.append(rec)
            except Exception as exc:                   # noqa: BLE001
                # A DRIVER-SIDE FAILURE IS NOT A SERVICE TIMEOUT and is
                # recorded as its own thing. Folding them together would let a
                # broken driver report itself as a blown latency budget, which
                # is precisely the finding experiment 1 is looking for.
                self.out.append({"status": 0, "latency_us": None,
                                 "driver_error": type(exc).__name__})
                try:
                    self.conn.close()
                    self.conn = http.client.HTTPConnection(HOST, PORT,
                                                           timeout=120)
                except Exception:                      # noqa: BLE001
                    pass
            finally:
                self.work.task_done()


def run(requests, concurrency, path="/authorize"):
    """Send every request with `concurrency` workers. Returns the records.

    The result order is not the request order, deliberately. Nothing
    downstream may depend on it: a concurrent driver exists so that
    completion order is not submission order, and a caller that assumed
    otherwise would be reading a different experiment's data.
    """
    work = queue.Queue(maxsize=concurrency * 4)
    out = []
    ready = threading.Barrier(concurrency + 1)
    workers = [Worker(work, out, path, ready) for _ in range(concurrency)]
    for w in workers:
        w.start()
    ready.wait()                       # every connection is open and warm
    t0 = time.perf_counter()
    for r in requests:
        work.put(r)
    for _ in workers:
        work.put(None)
    work.join()
    for w in workers:
        w.join(timeout=10)
    wall_s = time.perf_counter() - t0
    return out, wall_s


def noop_floor(n, concurrency):
    """What an empty request costs in this harness.

    /healthz touches nothing: no connection, no cache, no lock. Every other
    number in experiment 1 is only meaningful above this, and publishing it is
    what lets a reader subtract the harness instead of trusting that it is
    small.
    """
    out, wall = run(["GET"] * n, concurrency, path="/healthz")
    lat = [r["latency_us"] for r in out if r["latency_us"] is not None]
    s = summarize(lat)
    s["requests_per_second"] = round(len(out) / wall, 1)
    s["errors"] = sum(1 for r in out if r["status"] != 200)
    return s


def load_events(limit=None, offset=0, include_replays=True):
    """The arrival stream, in order, as request bodies.

    Replays are excluded for the experiments that count errors, and included
    for the one that is about them. A replay is the same event delivered
    again, so scoring it counts one provider's coverage decision twice and
    quietly weights the busiest events higher in every accuracy figure.
    Experiment 4 is the only place the duplicate deliveries are the subject.
    """
    sql = """
        SELECT a.seq, a.event_id, a.is_replay, e.member_id, e.merchant_id,
               e.plan_id, e.covered, e.offered_at_ms
          FROM truth.arrival a
          JOIN truth.event e ON e.event_id = a.event_id
    """
    if not include_replays:
        sql += " WHERE NOT a.is_replay"
    sql += " ORDER BY a.seq"
    if limit is not None:
        sql += " OFFSET %d LIMIT %d" % (offset, limit)
    return lab.query_json(sql)


def to_request(ev):
    return {"event_id": ev["event_id"], "member_id": ev["member_id"],
            "merchant_id": ev["merchant_id"], "plan_id": ev["plan_id"]}


def score(records, events_by_id):
    """Count the two error kinds against the answer key.

    WRONGLY TURNED AWAY and WRONGLY BILLED are the two costs the whole
    repository is about, and they are counted here and nowhere else so that
    every experiment prices the same numbers.
    """
    approved_ok = denied_ok = wrongly_denied = wrongly_approved = 0
    by_source = {}
    for r in records:
        if r.get("status") != 200 or "approved" not in r:
            continue
        truth = events_by_id.get(r["event_id"])
        if truth is None:
            continue
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        if r["approved"] and truth["covered"]:
            approved_ok += 1
        elif not r["approved"] and not truth["covered"]:
            denied_ok += 1
        elif r["approved"] and not truth["covered"]:
            wrongly_approved += 1          # an unexpected bill
        else:
            wrongly_denied += 1            # turned away at the front desk
    total = approved_ok + denied_ok + wrongly_denied + wrongly_approved
    return {
        "scored": total,
        "correct": approved_ok + denied_ok,
        "wrongly_denied": wrongly_denied,
        "wrongly_approved": wrongly_approved,
        "accuracy": round((approved_ok + denied_ok) / total, 6) if total else None,
        "by_source": by_source,
    }
