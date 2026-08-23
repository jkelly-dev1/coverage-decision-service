"""The coverage decision service.

    POST /authorize   decide one authorization
    POST /config      change the operating parameters between runs
    GET  /config      read them back
    GET  /healthz     liveness, touching nothing

Run it with the compose file, not by hand. It needs Postgres and it reads its
connection settings from the environment.

What this is for. It is a subject for measurement, not a product. The three
things it does that matter are these: it has a LATENCY BUDGET and a policy for what
to say when the budget is blown, it has a CACHE that can be wrong, and it has
three different IDEMPOTENCY GUARDS so a replayed webhook can be shown to be
handled correctly or not.

Every operating parameter is settable at runtime, through /config. That is
deliberate: an experiment that has to rebuild an image between sweep points
measures the rebuild as much as the parameter, and a reader cannot tell which
build produced which number.
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service import db as dbmod
from service.decide import Config, Cache, decide


def _env():
    return {
        "host": os.environ.get("CDS_PGHOST", "127.0.0.1"),
        "port": int(os.environ.get("CDS_PGPORT", "5432")),
        "user": os.environ.get("CDS_PGUSER", "coverage-local"),
        "password": os.environ.get("CDS_PGPASSWORD", "coverage-local-secret"),
        "dbname": os.environ.get("CDS_PGDATABASE", "coverage"),
    }


class State:
    """Everything the handler threads share, and the lock that guards it."""

    def __init__(self):
        self.config = Config()
        self.cache = Cache()
        self.pool = None
        self.lock = threading.Lock()
        self.counters = {}

    def bump(self, name, n=1):
        with self.lock:
            self.counters[name] = self.counters.get(name, 0) + n


STATE = State()


class Handler(BaseHTTPRequestHandler):
    # The default logger writes a line per request to stderr, which at a few
    # thousand requests per sweep point costs more than the work being
    # measured and interleaves badly across threads.
    def log_message(self, fmt, *args):
        pass

    protocol_version = "HTTP/1.1"

    # TCP_NODELAY, and it is not an optimization; it is the difference
    # between measuring this service and measuring nagle's algorithm.
    # BaseHTTPRequestHandler writes the headers and then the body as two
    # separate sends on an unbuffered socket. With Nagle on, the second send
    # is held until the peer acknowledges the first, and the peer's stack
    # delays that acknowledgement by about 40 ms. A request that touches
    # nothing then takes 42 ms, which is forty times what a request that
    # queries the database takes.
    disable_nagle_algorithm = True

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        if self.path == "/healthz":
            # Touches nothing on purpose. It is the no-op floor experiment 1
            # measures the rest of the service against, so it must not acquire
            # a connection, read the cache or take the lock.
            self._send(200, {"ok": True})
        elif self.path == "/config":
            self._send(200, STATE.config.as_dict())
        elif self.path == "/stats":
            with STATE.lock:
                self._send(200, {"counters": dict(STATE.counters),
                                 "cache_entries": STATE.cache.size()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            if self.path == "/config":
                body = self._read_json()
                with STATE.lock:
                    STATE.config.update(body)
                    if body.get("clear_cache"):
                        STATE.cache.clear()
                    if body.get("reset_counters"):
                        STATE.counters = {}
                self._send(200, STATE.config.as_dict())
            elif self.path == "/authorize":
                self._send(200, decide(self._read_json(), STATE))
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:                      # noqa: BLE001
            # A HANDLER THAT DIES SILENTLY LOOKS LIKE A TIMEOUT to the load
            # driver, and a timeout is exactly what experiment 1 is measuring.
            # The two must never be confused, so a fault here is reported as a
            # 500 with its type and counted separately.
            STATE.bump("handler_error")
            self._send(500, {"error": type(exc).__name__, "detail": str(exc)})


class Server(ThreadingHTTPServer):
    # The default listen backlog is five. A load driver opens all of its
    # connections at once, so with 64 workers most of the SYNs arrive while
    # the accept queue is full, the kernel drops them, and the client's stack
    # retries after one second and then two. That put 1.4-second outliers in
    # the no-op floor, a request that touches nothing, waiting on TCP
    # retransmission backoff, and nothing about it is a property of the
    # service being measured.
    request_queue_size = 256
    daemon_threads = True


def main():
    settings = _env()
    STATE.pool = dbmod.Pool(settings, size=int(
        os.environ.get("CDS_POOL_SIZE", "16")))
    STATE.pool.wait_ready(timeout_s=60)
    server = Server(("0.0.0.0", 8080), Handler)
    print("listening on 8080, pool=%d" % STATE.pool.size, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
