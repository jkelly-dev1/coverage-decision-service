"""Shared helpers: reaching Postgres, reaching the service, writing a result.

POSTGRES is reached through `docker exec` and the service through HTTP. The
scripts in this directory are the EXPERIMENT HARNESS, not the service, so they
are held to the same rule the test suite is: standard library only. Nothing
here needs psycopg, and a reader can run every experiment with a bare
interpreter and Docker.
"""

import json
import os
import subprocess
import urllib.error
import urllib.request

CONTAINER = "cds-postgres"
DB_USER = "coverage-local"
DB_NAME = "coverage"
SERVICE = "http://127.0.0.1:18080"

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
RESULTS = os.path.join(REPO, "results")


class PsqlError(RuntimeError):
    pass


def _run(args, stdin=None):
    p = subprocess.run(args, input=stdin, capture_output=True, text=True)
    if p.returncode != 0:
        raise PsqlError((p.stderr or p.stdout).strip())
    if "ERROR:" in p.stderr:
        raise PsqlError(p.stderr.strip())
    return p.stdout


def psql(sql, quiet=True):
    args = ["docker", "exec", "-i", CONTAINER, "psql",
            "-v", "ON_ERROR_STOP=1", "-U", DB_USER, "-d", DB_NAME]
    if quiet:
        args += ["-q"]
    return _run(args + ["-f", "-"], stdin=sql)


def psql_file(path):
    with open(path, encoding="utf-8") as fh:
        return psql(fh.read())


def scalar(sql):
    out = _run(["docker", "exec", "-i", CONTAINER, "psql",
                "-v", "ON_ERROR_STOP=1", "-U", DB_USER, "-d", DB_NAME,
                "-t", "-A", "-f", "-"], stdin=sql)
    return out.strip()


def wrap_json(sql):
    """Wrap a sub-query so Postgres serializes the rows.

    The closing paren is on its own line, and that is not formatting. A
    sub-query whose last line is a `--` comment swallows everything after it
    up to the next newline, so `FROM (%s) t;` written on one line hands
    Postgres a statement with an unclosed paren and no terminator; psql then
    waits for more input rather than failing, which is the worst of the three
    possible outcomes.
    """
    return ("SELECT coalesce(json_agg(t), '[]'::json)::text "
            "FROM (%s\n) t;" % sql.rstrip().rstrip(";"))


def query_json(sql):
    """Rows as a list of dicts. Postgres serializes, not psql: a text-mode
    parse would guess at NULL against the empty string."""
    return json.loads(scalar(wrap_json(sql)))


def copy_rows(table, columns, rows):
    """Load rows through psql's \\copy, which reads on THIS side.

    The container cannot see the repository, so a server-side COPY would need
    a mount and would silently read a stale file if the mount were forgotten.
    """
    def cell(v):
        if v is None:
            return "\\N"
        if v is True:
            return "t"
        if v is False:
            return "f"
        return str(v)

    payload = "\n".join("\t".join(cell(r.get(c)) for c in columns)
                        for r in rows).encode("utf-8")
    cmd = "\\copy %s (%s) FROM STDIN" % (table, ", ".join(columns))
    p = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", "-v", "ON_ERROR_STOP=1",
         "-U", DB_USER, "-d", DB_NAME, "-c", cmd],
        input=payload, capture_output=True)
    if p.returncode != 0:
        raise PsqlError(p.stderr.decode().strip())
    return p.stdout.decode().strip()


# ---------------------------------------------------------------------------
# THE SERVICE
# ---------------------------------------------------------------------------

def post(path, body, timeout=30):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(SERVICE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path, timeout=30):
    with urllib.request.urlopen(SERVICE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def configure(**kwargs):
    """Set operating parameters and READ THEM BACK.

    The service returns its whole config, and every caller compares what it
    asked for against what it got. A sweep that silently failed to apply a
    parameter would otherwise produce a full set of measurements of the
    previous setting.
    """
    got = post("/config", kwargs)
    for k, v in kwargs.items():
        if k in ("clear_cache", "reset_counters"):
            continue
        if got.get(k) != v:
            raise RuntimeError(
                "config not applied: asked %s=%r, service reports %r"
                % (k, v, got.get(k)))
    return got


def service_ready(attempts=60):
    import time
    for _ in range(attempts):
        try:
            if get("/healthz").get("ok"):
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1.0)
    return False


# ---------------------------------------------------------------------------

def input_manifest():
    import sys
    sys.path.insert(0, HERE)
    import generate_traffic as gen
    return gen.manifest(*gen.build())["sha256"]


def write_result(name, payload):
    payload = dict(payload, input_manifest_sha256=input_manifest())
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, name + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


class MissingResult(RuntimeError):
    """A results file a script needs is not on disk.

    Raised instead of letting FileNotFoundError escape, so a caller reports
    the missing file and the command that writes it, not a traceback naming a
    line in this file. A checker whose failure mode is a stack trace teaches a
    reader to skim it.
    """

    def __init__(self, name, path):
        self.name = name
        self.path = path
        # Every results file here is named for the script that writes it, so
        # the remedy can be derived rather than looked up.
        super().__init__(
            "results/%s.json is missing (looked in %s).\n"
            "Nothing has been checked. Produce it first:\n"
            "    cd stack && docker compose up -d && cd ..\n"
            "    python3 scripts/load.py\n"
            "    python3 scripts/%s.py" % (name, path, name))


# The four experiments, in the order they are run. Named once so that a script
# asking "what was everything measured on" cannot silently ask about three.
RESULT_NAMES = ("exp1_degradation", "exp2_fail_open_closed",
                "exp3_cache_and_kill_switch", "exp4_idempotency")


def read_result(name):
    path = os.path.join(RESULTS, name + ".json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise MissingResult(name, path) from None


def shipped_input_manifests():
    """The input manifest sha256 of every shipped results file, by name.

    Returns {} when there are none to read, and the caller has to say so out
    loud rather than treat an empty comparison as a passing one.
    """
    out = {}
    for name in RESULT_NAMES:
        try:
            out[name] = read_result(name)["input_manifest_sha256"]
        except MissingResult:
            continue
    return out


def manifest_disagreements(recomputed_sha, shipped):
    """Which shipped results were measured on a DIFFERENT world than this one.

    Pure, and separate from the reading, so it can be tested without a results
    directory and so the refusal it feeds cannot be a function of the network,
    the container, or what happens to be on disk.
    """
    return sorted(name for name, sha in shipped.items()
                  if sha != recomputed_sha)


def service_pool_size():
    """The pool the RUNNING service actually built, or None if it will not say.

    The size comes from CDS_POOL_SIZE at startup and is not a config field, so
    /config cannot answer this and lab.configure's read-back never touched it.
    Experiment 1's whole premise is that concurrency is above the pool, so
    the pool's size is read from the service rather than from a constant in
    the experiment script.
    """
    return get("/stats").get("pool_size")


# ---------------------------------------------------------------------------
# The scored predictions, enumerated in one place
# ---------------------------------------------------------------------------

def scored_predictions(e1, e2, e3, e4):
    """Every prediction this repository SCORED, with the verdict it got.

    Enumerated here so the README's count of refuted predictions is derived:
    a count of a repository's own refutations reads like a summary rather
    than a measurement, so it is the figure least likely to be re-derived.

    What counts as one prediction. Experiment 1 states a single claim and
    scores it twice, once per arrangement (its own `note` says so), and the
    two arrangement scorings are the atomic verdicts here. The experiment's
    top-level verdict is REFUTED because it requires both to hold, so counting
    it as well would count the same evidence twice.

    Not included, and this is the other half of getting the count right: the
    expectation in experiment 4 that the duplicates check-then-insert lets
    through would also DISAGREE with each other. It is described in the README
    and it was wrong in the interesting direction, but no script scores it and
    no results file records a verdict for it, so it is not one of these.
    """
    p1 = e1["prediction"]
    return [
        ("a timeout bounds the tail, budget covering the queue",
         p1["with_budget_covering_the_queue"]["verdict"]),
        ("a timeout bounds the tail, statement timeout alone",
         p1["with_a_statement_timeout_alone"]["verdict"]),
        ("the winning policy flips inside the swept price range",
         e2["prediction"]["verdict"]),
        ("no TTL buys hit rate for free",
         e3["predictions"]["no_ttl_buys_hit_rate_for_free"]["verdict"]),
        ("a longer TTL never argues against itself on latency",
         e3["predictions"]["longer_is_better_for_latency"]["verdict"]),
        ("check-then-insert makes the authorize endpoint idempotent",
         e4["prediction"]["verdict"]),
    ]


# Spelled out because the README spells them out. A sentence that reads "3 of
# the 6" would be derivable without this and would also not be the sentence
# anybody wrote.
NUMBER_WORDS = {0: "none", 1: "one", 2: "two", 3: "three", 4: "four",
                5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine",
                10: "ten"}
