"""Make the service and the scripts importable, and load the shipped evidence.

The suite is offline and stays offline. Reproducing the measurements needs
Postgres, the service container and several minutes of wall clock; asserting
them does not. Every test here exercises either a pure function or the
committed results/*.json, so CI installs pytest and nothing else and never
starts a container.

Service/db.py is not imported anywhere in this suite, and that is deliberate
rather than an oversight: it is the one module that needs psycopg, so
importing it would put a third-party package in CI's way for no benefit. The
decision logic it serves lives in service/decide.py, and decide() itself is
exercised in tests/test_decide_path.py against a fake pool. The pieces alone
were tested for a long time while the function that composes them was not.
"""

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))


def _result(name):
    with open(os.path.join(REPO, "results", name + ".json"),
              encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def exp1():
    return _result("exp1_degradation")


@pytest.fixture(scope="session")
def exp2():
    return _result("exp2_fail_open_closed")


@pytest.fixture(scope="session")
def exp3():
    return _result("exp3_cache_and_kill_switch")


@pytest.fixture(scope="session")
def exp4():
    return _result("exp4_idempotency")
