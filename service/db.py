"""The connection pool and the three queries the service makes.

The Pool is written out rather than imported. Experiment 1 asks whether a
timeout bounds the tail, and the answer depends entirely on whether the
budget covers ONLY the query or also the wait for a connection. A pool hidden
behind a library would make that unanswerable; here it is forty lines and the
accounting is visible.

Both arrangements are implemented, selected by `budget_covers_queue`. With it
on, the deadline starts when the request arrives and the queue is spent out of
the same budget: a caller told 50 ms gets an answer in 50 ms. With it off, the
request waits as long as it takes for a connection and THEN gives the query
the full timeout, which is what a statement timeout alone actually buys.

Neither is a straw man and the difference is invisible until the Pool
saturates. Below saturation the two produce the same numbers, so this is a
flag and a measurement rather than a rule stated once.
"""

import queue
import threading
import time

import psycopg


class Pool:
    """A fixed set of connections handed out under a deadline."""

    def __init__(self, settings, size=16):
        self.size = size
        self.settings = settings
        self._free = queue.LifoQueue()
        # A LIFO queue, not FIFO. Reusing the most recently returned
        # connection keeps a small number of them warm rather than cycling
        # every connection through every request, which matters because a
        # connection carries the session state this file caches on it.
        self._timeout_state = {}
        self._lock = threading.Lock()
        for _ in range(size):
            self._free.put(self._connect())

    def _connect(self):
        conn = psycopg.connect(
            host=self.settings["host"], port=self.settings["port"],
            user=self.settings["user"], password=self.settings["password"],
            dbname=self.settings["dbname"], autocommit=True)
        return conn

    def wait_ready(self, timeout_s=60):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                conn = self._free.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                return True
            finally:
                self._free.put(conn)
        raise RuntimeError("database never became ready")

    def _acquire(self, deadline):
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError("budget spent before a connection was free")
        try:
            return self._free.get(timeout=remaining)
        except queue.Empty:
            # The queue is where the budget goes under load, and this is the
            # branch that says so. It is a TimeoutError like any other, and
            # the caller counts it the same way, because from the member's
            # side there is no difference between a slow database and a
            # database you could not get to.
            raise TimeoutError("no connection within the budget")

    def _release(self, conn):
        self._free.put(conn)

    def _set_timeout(self, conn, ms):
        """SET statement_timeout, but only when it has actually changed.

        A SET is a round trip. Issuing one before every query would double the
        round trips per request and put that cost inside the very measurement
        this repository is about. The value is constant within a sweep point,
        so this costs one SET per connection per sweep point.
        """
        if self._timeout_state.get(id(conn)) == ms:
            return
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = %s" % int(ms))
        self._timeout_state[id(conn)] = ms

    # ---- the queries -------------------------------------------------------

    COVERAGE_SQL = """
        SELECT pg_sleep(%(delay_s)s),
               (SELECT covered
                  FROM coverage.network_status
                 WHERE plan_id = %(plan)s AND merchant_id = %(merchant)s
                 ORDER BY effective_from DESC
                 LIMIT 1) AS covered
    """

    def coverage(self, plan_id, merchant_id, timeout_ms, inject_delay_ms=0,
                 budget_covers_queue=True):
        """Is this plan covering this merchant, right now?

        That injected delay is inside the scalar subquery's sibling, not
        inside the lookup. Pg_sleep in the target list of the lookup itself
        would not run at all when no row matches, so every non-provider
        merchant would skip the fault injection and the latency distribution
        would have a fast mode nobody put there.

        A merchant with no row is not covered, and that is a decision rather
        than an accident: absence is the answer for the ordinary businesses a
        card also touches, and returning an error for them would make the
        service fall back on its most common input.
        """
        deadline = time.perf_counter() + timeout_ms / 1000.0
        if budget_covers_queue:
            conn = self._acquire(deadline)
        else:
            # The naive arrangement, and it is not a straw man. Waiting for a
            # connection and then timing the query is what a pool's own
            # default behavior looks like in most libraries: the timeout you
            # configured is a STATEMENT timeout, and nothing bounds the queue
            # ahead of it. Every database call respects the budget and the
            # caller does not.
            conn = self._free.get()
        try:
            if budget_covers_queue:
                remaining_ms = int((deadline - time.perf_counter()) * 1000)
                if remaining_ms <= 0:
                    raise TimeoutError("budget spent in the queue")
            else:
                remaining_ms = int(timeout_ms)
            self._set_timeout(conn, remaining_ms)
            with conn.cursor() as cur:
                cur.execute(self.COVERAGE_SQL,
                            {"delay_s": inject_delay_ms / 1000.0,
                             "plan": plan_id, "merchant": merchant_id})
                row = cur.fetchone()
            return bool(row[1])
        except psycopg.errors.QueryCanceled:
            # statement_timeout fired. The connection is usable again, but the
            # transaction has to be cleared first or the next caller inherits
            # an aborted one.
            try:
                conn.rollback()
            except Exception:                          # noqa: BLE001
                pass
            raise TimeoutError("statement timeout")
        finally:
            self._release(conn)

    def prior_decision(self, run_id, guard, event_id):
        """The decision already recorded for this event, or None.

        The check half of check-then-insert, and it is deliberately a separate
        statement from the insert. That gap is the entire subject of
        experiment 4: two deliveries of the same event can both pass this
        check before either of them writes.
        """
        conn = self._free.get()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT approved FROM coverage.decision"
                    " WHERE run_id = %s AND guard = %s AND event_id = %s"
                    " LIMIT 1", (run_id, guard, event_id))
                row = cur.fetchone()
            return None if row is None else {"approved": bool(row[0])}
        finally:
            self._release(conn)

    INSERT_PLAIN = """
        INSERT INTO coverage.decision
            (run_id, guard, event_id, member_id, merchant_id, approved,
             source, latency_us, decided_at_ms)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    INSERT_UNIQUE = """
        INSERT INTO coverage.decision_unique
            (run_id, guard, event_id, member_id, merchant_id, approved,
             source, latency_us, decided_at_ms)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (run_id, event_id) DO NOTHING
        RETURNING approved
    """

    def record(self, cfg, event_id, member_id, merchant_id, approved, source,
               latency_us):
        """Write the decision. Returns (inserted, the decision that stands).

        The unique variant returns the original decision rather than raising.
        A guard that refuses the second delivery has not made the endpoint
        idempotent, it has made it fragile: the processor retries because it
        did not hear back, and answering the retry with an error guarantees it
        retries again.
        """
        now_ms = int(time.time() * 1000)
        args = (cfg.run_id, cfg.guard, event_id, member_id, merchant_id,
                bool(approved), source, latency_us, now_ms)
        conn = self._free.get()
        try:
            with conn.cursor() as cur:
                if cfg.guard == "unique":
                    cur.execute(self.INSERT_UNIQUE, args)
                    row = cur.fetchone()
                    if row is not None:
                        return True, bool(row[0])
                    cur.execute(
                        "SELECT approved FROM coverage.decision_unique"
                        " WHERE run_id = %s AND event_id = %s",
                        (cfg.run_id, event_id))
                    existing = cur.fetchone()
                    return False, bool(existing[0])
                cur.execute(self.INSERT_PLAIN, args)
                return True, bool(approved)
        finally:
            self._release(conn)
