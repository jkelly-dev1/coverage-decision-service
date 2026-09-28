"""The connection pool, driven by fake connections instead of Postgres.

service/db.py imports psycopg, which the offline suite does not install, so
the module is imported against the same stand-in the handler tests use, and
every connection the pool makes is a fake that records the SQL it was sent.
"""

import importlib
import importlib.util
import sys

import pytest

from test_http_handler import _stub_psycopg


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append(sql.strip())

    def fetchone(self):
        return (1, self.conn.answer)


class FakeConn:
    made = 0

    def __init__(self, answer=True):
        FakeConn.made += 1
        self.answer = answer
        self.closed = False
        self.sql = []

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        pass


@pytest.fixture
def db(monkeypatch):
    if importlib.util.find_spec("psycopg") is None:
        monkeypatch.setitem(sys.modules, "psycopg", _stub_psycopg())
    module = importlib.import_module("service.db")
    monkeypatch.setattr(module.Pool, "_connect", lambda self: FakeConn())
    return module


def test_a_connection_the_server_dropped_is_replaced_when_it_is_returned(db):
    pool = db.Pool({}, size=1)
    conn = pool._free.get()
    conn.closed = True
    pool._release(conn)
    again = pool._free.get()
    assert again is not conn and again.closed is False


def test_the_coverage_answer_is_the_row_the_query_returned(db):
    pool = db.Pool({}, size=1)
    assert pool.coverage("P1", 7, timeout_ms=50) is True
    conn = pool._free.get()
    conn.answer = False
    pool._release(conn)
    assert pool.coverage("P1", 7, timeout_ms=50) is False


def test_a_zero_budget_is_enforced_rather_than_disabling_the_timeout(db):
    """statement_timeout = 0 means no timeout at all in Postgres."""
    pool = db.Pool({}, size=1)
    pool.coverage("P1", 7, timeout_ms=0, budget_covers_queue=False)
    conn = pool._free.get()
    assert "SET statement_timeout = 1" in conn.sql, conn.sql
    assert "SET statement_timeout = 0" not in conn.sql


def test_a_replacement_connection_gets_its_own_statement_timeout(db, monkeypatch):
    """The timeout cache is keyed by id(conn), and CPython hands a freed
    address to the next object. A replacement at the dead connection's
    address must still be sent its SET, or it runs with no timeout at all."""
    pool = db.Pool({}, size=1)
    pool.coverage("P1", 7, timeout_ms=50)
    dead = pool._free.get()
    assert any("statement_timeout" in s for s in dead.sql)
    dead.closed = True

    def reborn(self):
        # A new connection that happens to land at the same address.
        dead.closed, dead.sql = False, []
        return dead

    monkeypatch.setattr(db.Pool, "_connect", reborn)
    pool._release(dead)
    pool.coverage("P1", 7, timeout_ms=50)
    assert any("statement_timeout" in s for s in dead.sql), dead.sql
