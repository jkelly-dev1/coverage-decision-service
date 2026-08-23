"""The decision itself, and everything about it that can be tested offline.

This file holds the logic and not the plumbing, on purpose. Every rule that
decides what a member is told lives here as a pure function or as a small
class with an injectable clock, so tests/ can exercise all of it with pytest
alone and no container. The database access it needs is passed in.

The three decisions this service makes, and each one is a published
measurement rather than a default:

  What to say when the budget is blown. Fail-open approves and sends somebody
  an unexpected bill; fail-closed denies and turns a covered member away at
  the front desk. Neither is correct from first principles.

  How long to trust a cached answer. A longer TTL is faster and wrong for
  longer, and a stale entry is a wrong answer no timeout and no fallback
  policy can see.

  What a replayed webhook gets. Returning the ORIGINAL decision is the
  requirement. A guard that raises on the second delivery has not made the
  endpoint idempotent, it has made it fragile.
"""

import threading
import time

# The sources a decision can carry. Written down because every count in
# results/ is grouped by this and a typo would silently create a category.
SRC_DB = "db"
SRC_CACHE = "cache"
SRC_FALLBACK_OPEN = "fallback_open"
SRC_FALLBACK_CLOSED = "fallback_closed"
SRC_KILL_SWITCH = "kill_switch"
SRC_REPLAY = "replay"

GUARD_NONE = "none"
GUARD_CHECK_THEN_INSERT = "check_then_insert"
GUARD_UNIQUE = "unique"


class Config:
    """The operating parameters, all settable at runtime through /config."""

    FIELDS = {
        "run_id": "default",
        # The latency budget, in milliseconds. The service will not wait
        # longer than this for the datastore before falling back.
        "timeout_ms": 50,
        # Whether the budget covers the wait for a connection or only the
        # query. This is the whole of experiment 1 and it is a flag rather
        # than a decision because both implementations exist in the world and
        # the difference between them is invisible until the pool saturates.
        #
        # True  the deadline starts when the request arrives; time spent
        #       queueing for a connection is spent out of the same budget.
        # False the naive arrangement: wait as long as it takes for a
        #       connection, THEN give the query the full timeout. Every
        #       individual database call respects the budget and the caller
        #       can still wait arbitrarily long.
        "budget_covers_queue": True,
        # 'open' or 'closed'. What to answer when the budget is blown.
        "fallback": "closed",
        # 0 disables the cache entirely.
        "cache_ttl_ms": 0,
        # When true the service answers without touching the datastore at all.
        "kill_switch": False,
        # What the kill switch answers. Kept separate from `fallback` because
        # they are different decisions: one is what to do when the dependency
        # is slow, the other is what to do when an operator has decided to
        # stop depending on it.
        "kill_switch_answer": False,
        # Injected datastore delay, in milliseconds. The fault injection.
        "inject_db_delay_ms": 0,
        "guard": GUARD_NONE,
        # Whether to write the decision at all. Experiment 1 measures latency
        # and does not need the write; experiment 4 is entirely about it.
        "record": True,
    }

    def __init__(self):
        for k, v in self.FIELDS.items():
            setattr(self, k, v)

    def update(self, body):
        for k, v in body.items():
            if k in self.FIELDS:
                setattr(self, k, v)

    def as_dict(self):
        return {k: getattr(self, k) for k in self.FIELDS}


class Cache:
    """A TTL cache keyed on (plan_id, merchant_id).

    The clock is injectable so the TTL can be tested without sleeping. A test
    that proves an expiry by waiting is slow, flaky, and proves it only for
    the one duration it waited.

    No eviction by size. The working set here is a few thousand entries and a
    size bound would add a second reason for a miss, which would make the hit
    rate in experiment 3 a function of two things instead of one.
    """

    def __init__(self, clock=None):
        self._clock = clock or (lambda: time.time() * 1000.0)
        self._data = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.expired = 0

    def get(self, key, ttl_ms):
        if ttl_ms <= 0:
            return None
        now = self._clock()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None
            value, stored_at = entry
            if now - stored_at > ttl_ms:
                # Expiry is counted separately from a cold miss. They cost the
                # same latency and mean completely different things: one is a
                # cache that has not warmed, the other is a TTL doing its job.
                self.expired += 1
                self.misses += 1
                del self._data[key]
                return None
            self.hits += 1
            return value

    def put(self, key, value):
        with self._lock:
            self._data[key] = (value, self._clock())

    def clear(self):
        with self._lock:
            self._data.clear()
            self.hits = self.misses = self.expired = 0

    def size(self):
        with self._lock:
            return len(self._data)

    def stats(self):
        with self._lock:
            total = self.hits + self.misses
            return {"hits": self.hits, "misses": self.misses,
                    "expired": self.expired,
                    "hit_rate": round(self.hits / total, 6) if total else 0.0}


def fallback_answer(fallback):
    """What to say when the budget is blown.

    Raises on an unknown policy rather than defaulting. A typo in a config
    sweep would otherwise silently pick one of the two policies the experiment
    is comparing, and the run would look complete.
    """
    if fallback == "open":
        return True, SRC_FALLBACK_OPEN
    if fallback == "closed":
        return False, SRC_FALLBACK_CLOSED
    raise ValueError("unknown fallback policy: %r" % (fallback,))


def decide(request, state):
    """One authorization decision. Returns the response body.

    The order of these branches is the design. The kill switch is checked
    before the cache, and the cache before the datastore, because each is a
    way of not doing the more expensive thing below it. A kill switch that
    still consulted the cache would not be a kill switch.
    """
    cfg = state.config
    started = time.perf_counter()

    event_id = request["event_id"]
    member_id = int(request["member_id"])
    merchant_id = int(request["merchant_id"])
    plan_id = request["plan_id"]

    # ---- an already-decided event gets its ORIGINAL answer back ------------
    #
    # The check is here, at the top, and the write is at the bottom. That is
    # how the guard is actually written in practice: look first, and skip all
    # the work if this event has already been answered. It is also why the
    # race window is the whole body of this function rather than two adjacent
    # statements, which is what experiment 4 measures.
    if cfg.record and cfg.guard == GUARD_CHECK_THEN_INSERT:
        prior = state.pool.prior_decision(cfg.run_id, cfg.guard, event_id)
        if prior is not None:
            state.bump("replay_served_from_prior")
            return _respond(prior["approved"], SRC_REPLAY, started, event_id,
                            replayed=True)

    # ---- the kill switch ---------------------------------------------------
    if cfg.kill_switch:
        approved, source = bool(cfg.kill_switch_answer), SRC_KILL_SWITCH
        state.bump("kill_switch")
    else:
        key = (plan_id, merchant_id)
        cached = state.cache.get(key, cfg.cache_ttl_ms)
        if cached is not None:
            approved, source = cached, SRC_CACHE
            state.bump("cache_hit")
        else:
            try:
                approved = state.pool.coverage(
                    plan_id, merchant_id,
                    timeout_ms=cfg.timeout_ms,
                    inject_delay_ms=cfg.inject_db_delay_ms,
                    budget_covers_queue=cfg.budget_covers_queue)
                source = SRC_DB
                state.bump("db_hit")
                if cfg.cache_ttl_ms > 0:
                    state.cache.put(key, approved)
            except TimeoutError:
                approved, source = fallback_answer(cfg.fallback)
                state.bump("timeout")
            except Exception:                          # noqa: BLE001
                # A DEPENDENCY THAT ERRORS IS NOT A DEPENDENCY THAT IS SLOW,
                # and they are counted apart even though the fallback is the
                # same. Folding them together would make a broken pool look
                # like a blown latency budget.
                approved, source = fallback_answer(cfg.fallback)
                state.bump("db_error")

    if cfg.record:
        inserted, standing = state.pool.record(
            cfg, event_id, member_id, merchant_id, approved, source,
            int((time.perf_counter() - started) * 1e6))
        if not inserted:
            # The unique constraint found an existing row. The original
            # decision is what the caller gets, because that is what makes the
            # endpoint idempotent rather than merely protected.
            #
            # Detected by whether a row was inserted, not by comparing the two
            # answers. Roughly two thirds of replays would agree with their
            # original by chance, and a comparison would report those as fresh
            # decisions and undercount the replays it caught.
            state.bump("replay_served_from_prior")
            return _respond(standing, SRC_REPLAY, started, event_id,
                            replayed=True)

    return _respond(approved, source, started, event_id, replayed=False)


def _respond(approved, source, started, event_id, replayed):
    return {
        "event_id": event_id,
        "approved": bool(approved),
        "source": source,
        "replayed": replayed,
        "latency_us": int((time.perf_counter() - started) * 1e6),
    }
