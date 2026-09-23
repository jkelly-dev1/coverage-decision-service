"""The experiment harness's own refusals, and its failure messages.

The scripts in `scripts/` are not the subject of this repository, but two of
the things they do decide whether a published number means anything:

  * Experiment 1 refuses to run unless the service's real connection pool is
    the one the README describes and is smaller than the concurrency. Its
    headline, the same timeout producing a tail 10.8 times longer, is only a
    finding above pool saturation, and below saturation the two
    arrangements it compares are indistinguishable.

  * The README checker says what is missing instead of dying inside a helper
    with a traceback, which would fail in the right direction and teach a
    reader to skim the output.

Both are tested here rather than in the experiment scripts' own files because
neither needs Docker, Postgres or the service: the pool check is a pure
function of three numbers, and the missing-file message is a pure function of
a name.
"""

import io
from contextlib import redirect_stderr, redirect_stdout

import pytest

import check_readme_numbers
import load
import exp1_degradation as exp1
import lab


# ---------------------------------------------------------------------------
# Experiment 1 will not measure a pool it has not looked at
# ---------------------------------------------------------------------------

def test_the_pool_the_service_built_is_the_one_the_experiment_is_written_for():
    # The passing case: 16 connections, 64 workers.
    assert exp1.pool_size_complaint(16, 16, 64) is None


def test_a_pool_raised_by_the_environment_stops_the_run():
    """CDS_POOL_SIZE is read at startup by service/app.py and is not a /config
    field, so nothing the harness set could ever disagree with it. Raising it
    in stack/compose.yaml moved the experiment below saturation while the
    results file, the invariant test, the README and the figure checker all
    went on describing a pool of 16.
    """
    why = exp1.pool_size_complaint(128, 16, 64)
    assert why is not None
    assert "128" in why and "16" in why


def test_a_pool_at_or_above_the_concurrency_stops_the_run():
    # The condition the experiment exists under, stated as a refusal rather
    # than as a comment: at or above saturation is the only place the two
    # arrangements differ.
    assert exp1.pool_size_complaint(64, 64, 64) is not None
    assert exp1.pool_size_complaint(100, 100, 64) is not None


def test_a_service_that_will_not_say_stops_the_run_too():
    """The one that matters most, and the one an ordinary implementation gets
    wrong. Before /stats reported `pool_size` this call returned None, and a
    check written as `if reported != expected` would have read None as "no
    disagreement" and run anyway. A pre-flight that cannot look must refuse,
    not pass.
    """
    why = exp1.pool_size_complaint(None, 16, 64)
    assert why is not None
    assert "does not report" in why


# ---------------------------------------------------------------------------
# A checker that cannot look says so
# ---------------------------------------------------------------------------

def test_an_absent_results_file_is_a_sentence_and_not_a_traceback():
    with pytest.raises(lab.MissingResult,
                       match=r"results/exp9_does_not_exist\.json is missing"
                       ) as caught:
        lab.read_result("exp9_does_not_exist")
    message = str(caught.value)
    assert "results/exp9_does_not_exist.json is missing" in message
    assert "Nothing has been checked" in message
    # The remedy, not just the complaint. Every results file here is named for
    # the script that writes it.
    assert "python3 scripts/exp9_does_not_exist.py" in message


def test_the_checker_reports_a_missing_results_file_instead_of_dying(monkeypatch):
    """Patched on `lab`, which is the binding scripts/check_readme_numbers.py
    actually resolves: it does `import lab` and calls `lab.read_result`, so a
    patch of the name inside the checker's own namespace would change nothing
    it calls.

    The assertion is on an effect a no-op patch could not produce: the
    exit code AND the sentence on stderr AND the absence of the normal
    "figures re-derived" line, which is what a checker that had really run
    would print.
    """
    def refuse(name):
        raise lab.MissingResult(name, "/nowhere/%s.json" % name)

    monkeypatch.setattr(lab, "read_result", refuse)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = check_readme_numbers.main()

    assert rc == 1
    assert "exp1_degradation.json is missing" in err.getvalue()
    assert "figures re-derived" not in out.getvalue()


def test_the_checker_is_green_on_the_shipped_tree():
    # The control for the test above. Without it, a checker that returned 1
    # for every input would satisfy the missing-file test perfectly.
    out = io.StringIO()
    with redirect_stdout(out):
        rc = check_readme_numbers.main()
    assert rc == 0, out.getvalue()
    assert "all present" in out.getvalue()


# ---------------------------------------------------------------------------
# A refusal nothing invokes is a comment
# ---------------------------------------------------------------------------
#
# Both checks above are pure functions, which is what makes them testable at
# all, and is also how a gate quietly stops gating. `pool_size_complaint`
# would still return the right sentence for every input after its one call
# site was deleted, and every test above it would stay green. So the call
# sites are asserted too, structurally, against the parsed source.

def _main_of(module):
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(module))
    return [node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"][0]


def _names_called(node):
    import ast
    out = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                out.add(func.id)
            elif isinstance(func, ast.Attribute):
                out.add(func.attr)
    return out


def test_experiment_1_actually_asks_the_service_what_pool_it_built():
    called = _names_called(_main_of(exp1))
    assert "pool_size_complaint" in called, \
        "exp1's pool check is defined and never invoked"
    assert "service_pool_size" in called, \
        "exp1 never asks the service for its pool size"


def test_the_loader_compares_the_manifest_BEFORE_it_writes_anything():
    """Order matters here. A comparison made after the schema has been
    dropped and the rows streamed in has already done the damage it exists to
    prevent, and would leave a half-loaded world behind on the refusal.
    """
    import ast
    import load

    body = _main_of(load).body
    compared = writing = None
    read_them = False
    for i, stmt in enumerate(body):
        names = _names_called(stmt) if isinstance(stmt, ast.AST) else set()
        # Both halves, because either one alone is satisfiable by a gate that
        # does nothing: `shipped = {}` leaves the comparison call standing and
        # compares against an empty set, which never disagrees.
        if "shipped_input_manifests" in names:
            read_them = True
        if compared is None and "manifest_disagreements" in names:
            compared = i
        if writing is None and ("psql_file" in names or "copy_rows" in names):
            writing = i
    assert read_them, "load.py never reads the shipped manifests"
    assert compared is not None, "load.py never compares the manifest"
    assert writing is not None, "load.py never writes, so this test is wrong"
    assert compared < writing, \
        "load.py compares the manifest after it has already loaded"


# ---------------------------------------------------------------------------
# A sub-query wrapper a trailing SQL comment can eat
# ---------------------------------------------------------------------------

def test_a_subquery_ending_in_a_comment_still_closes_its_wrapper():
    """`FROM (%s) t;` on one line is a statement a `--` comment can swallow:
    everything after the comment marker up to the newline is gone, taking the
    closing paren and the semicolon with it. Postgres does not reject that
    (there is no error to see, because the statement is not finished), so
    psql sits waiting for the rest of it.

    The driver's own query in scripts/driver.py ends in a generated ORDER BY /
    OFFSET / LIMIT tail, so it does not end in a comment; a comment on the last
    line is one edit away.
    """
    wrapped = lab.wrap_json("SELECT 1 AS x  -- the row we want")
    assert wrapped.rstrip().endswith(") t;")
    last = wrapped.rstrip().splitlines()[-1]
    assert "--" not in last, \
        "the closing paren shares a line with a SQL comment and is eaten"


def test_the_wrapper_still_produces_one_statement_for_an_ordinary_query():
    # The control. A wrapper that emitted nothing at all would satisfy the
    # test above perfectly.
    wrapped = lab.wrap_json("SELECT a, b FROM t ORDER BY a;")
    assert wrapped.count(";") == 1
    assert "json_agg(t)" in wrapped
    assert "SELECT a, b FROM t ORDER BY a" in wrapped
