"""The arithmetic the experiment scripts publish, tested without the stack.

The scripts need a running service to MEASURE, but what they do with the
measurements is plain arithmetic, and a wrong line there would be published as
a new result the next time somebody re-ran them. Each function here is the
one the script calls.
"""

import re

import pytest

import exp1_degradation as exp1
import exp2_fail_open_closed as exp2
import exp3_cache_and_kill_switch as exp3
import exp4_idempotency as exp4
from service.decide import Config


def test_the_timeout_verdict_bound_is_the_budget_plus_the_floor():
    floor = 22435
    bound = exp1.TIMEOUT_MS * 1000 + floor
    inside = exp1.verdict([{"p99_us": bound}], floor)
    outside = exp1.verdict([{"p99_us": 10}, {"p99_us": bound + 1}], floor)
    assert inside["bound_us"] == bound and inside["within_bound"] is True
    assert outside["within_bound"] is False
    assert outside["worst_p99_us"] == bound + 1


def test_the_control_gap_is_the_difference_of_the_two_error_totals():
    rows = [
        {"inject_db_delay_ms": 0, "wrongly_denied": 149, "wrongly_approved": 3},
        {"inject_db_delay_ms": 0, "wrongly_denied": 0, "wrongly_approved": 275},
        {"inject_db_delay_ms": 25, "wrongly_denied": 999, "wrongly_approved": 0},
    ]
    assert exp2.control_gap(rows) == 123
    with pytest.raises(ValueError, match="one closed and one open run"):
        exp2.control_gap(rows[:1])


def test_staleness_is_scored_only_on_events_whose_coverage_changed():
    by_id = {"a": {"covered": True}, "b": {"covered": True},
             "c": {"covered": False}}
    records = [
        {"event_id": "a", "status": 200, "approved": False, "source": "cache"},
        {"event_id": "b", "status": 200, "approved": False, "source": "db"},
        {"event_id": "c", "status": 200, "approved": True, "source": "db"},
        {"event_id": "a", "status": 500},
    ]
    # Only "a" and "c" were affected by a change; "b" is wrong but cannot be
    # a staleness error, and the failed request is not a decision.
    assert exp3.score_affected(records, by_id, {"a", "c"}) == (2, 2, 1)


def test_the_removal_share_compares_the_guarded_and_unguarded_burst():
    assert exp4.removal_share({"duplicate_rows": 13},
                              {"duplicate_rows": 2850}) == pytest.approx(
        1 - 13 / 2850)
    with pytest.raises(ValueError, match="no duplicates to remove"):
        exp4.removal_share({"duplicate_rows": 0}, {"duplicate_rows": 0})


def test_every_published_port_is_bound_to_loopback():
    """The service process binds all interfaces inside its container, so the
    loopback claim rests on compose.yaml alone, and this reads it."""
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "stack", "compose.yaml")
    text = open(path, encoding="utf-8").read()
    bindings = re.findall(r'^\s*-\s*"([^"]*\d+:\d+)"\s*$', text, re.M)
    assert len(bindings) == 2, bindings
    assert all(b.startswith("127.0.0.1:") for b in bindings), bindings


def test_config_refuses_an_unknown_fallback_or_guard():
    cfg = Config()
    with pytest.raises(ValueError, match="fallback must be one of"):
        cfg.update({"fallback": "maybe"})
    with pytest.raises(ValueError, match="guard must be one of"):
        cfg.update({"guard": "hope"})
    assert cfg.fallback == "closed"
    cfg.update({"fallback": "open"})
    assert cfg.fallback == "open"
