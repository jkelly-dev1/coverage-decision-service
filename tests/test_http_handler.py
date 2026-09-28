"""The HTTP handler itself, driven over a real socket with no database.

service/app.py imports service/db.py, and service/db.py imports psycopg, the
one package this offline suite does not install. The handler never touches the
database on the paths tested here, so psycopg is replaced by a stand-in module
that refuses to connect, the module is imported with it, and a real
ThreadingHTTPServer on an ephemeral localhost port answers real requests. What
is exercised is the routing and the response shapes in Handler.do_GET and
Handler.do_POST, which experiment 1's load driver and every script in
scripts/ depend on.
"""

import http.client
import importlib
import importlib.util
import json
import sys
import threading
import types

import pytest


def _stub_psycopg():
    stub = types.ModuleType("psycopg")

    def connect(*args, **kwargs):
        raise AssertionError("the handler tests reached the database")

    class QueryCanceled(Exception):
        pass

    stub.connect = connect
    stub.errors = types.SimpleNamespace(QueryCanceled=QueryCanceled)
    return stub


@pytest.fixture
def app(monkeypatch):
    if importlib.util.find_spec("psycopg") is None:
        monkeypatch.setitem(sys.modules, "psycopg", _stub_psycopg())
    module = importlib.import_module("service.app")
    # A fresh State per test: the handler reads the module global at request
    # time, so this is the binding it resolves.
    monkeypatch.setattr(module, "STATE", module.State())
    return module


@pytest.fixture
def serve(app):
    server = app.Server(("127.0.0.1", 0), app.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1],
                                          timeout=5)
        data = None if body is None else json.dumps(body)
        headers = {} if body is None else {"Content-Type": "application/json"}
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, payload

    yield request
    server.shutdown()
    server.server_close()


def test_healthz_answers_without_touching_anything(app, serve):
    status, body = serve("GET", "/healthz")
    assert (status, body) == (200, {"ok": True})
    assert app.STATE.counters == {}


def test_get_config_returns_the_live_configuration(app, serve):
    status, body = serve("GET", "/config")
    assert status == 200
    assert body == app.STATE.config.as_dict()


def test_stats_reports_the_pool_the_process_built(app, serve):
    app.STATE.pool = types.SimpleNamespace(size=16)
    status, body = serve("GET", "/stats")
    assert status == 200
    assert body["pool_size"] == 16


def test_stats_reports_no_pool_as_null_and_not_by_omission(serve):
    status, body = serve("GET", "/stats")
    assert status == 200
    assert "pool_size" in body and body["pool_size"] is None


def test_an_unknown_path_is_a_404(serve):
    assert serve("GET", "/nope")[0] == 404
    assert serve("POST", "/nope", {})[0] == 404


def test_post_config_changes_the_configuration_it_reports(app, serve):
    before = app.STATE.config.as_dict()
    key, value = next((k, v) for k, v in before.items()
                      if isinstance(v, bool))
    status, body = serve("POST", "/config", {key: not value})
    assert status == 200
    assert body[key] is (not value)
    assert serve("GET", "/config")[1][key] is (not value)


def test_a_refused_config_value_is_a_400_and_not_a_handler_fault(app, serve):
    """A value Config.update refuses is the caller's mistake: a 400 naming
    it, the configuration unchanged, and no handler_error counted."""
    key, allowed = next(iter(app.STATE.config.CHOICES.items()))
    before = app.STATE.config.as_dict()
    status, body = serve("POST", "/config", {key: "no-such-" + allowed[0]})
    assert status == 400
    assert body["error"] == "ValueError" and key in body["detail"]
    assert app.STATE.config.as_dict() == before
    assert app.STATE.counters.get("handler_error") is None


def test_authorize_hands_the_request_body_to_decide(app, serve, monkeypatch):
    seen = []

    def fake_decide(request, state):
        seen.append((request, state))
        return {"approved": True, "source": "db"}

    monkeypatch.setattr(app, "decide", fake_decide)
    status, body = serve("POST", "/authorize", {"member_id": "M1"})
    assert (status, body) == (200, {"approved": True, "source": "db"})
    assert seen == [({"member_id": "M1"}, app.STATE)]


def test_a_fault_in_the_handler_is_a_500_and_is_counted(app, serve,
                                                         monkeypatch):
    # Not a timeout: the load driver must be able to tell the two apart.
    def broken(request, state):
        raise RuntimeError("boom")

    monkeypatch.setattr(app, "decide", broken)
    status, body = serve("POST", "/authorize", {})
    assert status == 500
    assert body == {"error": "RuntimeError", "detail": "boom"}
    assert app.STATE.counters.get("handler_error") == 1
