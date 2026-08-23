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


def query_json(sql):
    """Rows as a list of dicts. Postgres serializes, not psql: a text-mode
    parse would guess at NULL against the empty string."""
    wrapped = ("SELECT coalesce(json_agg(t), '[]'::json)::text "
               "FROM (%s) t;" % sql.rstrip().rstrip(";"))
    return json.loads(scalar(wrapped))


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


def read_result(name):
    with open(os.path.join(RESULTS, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)
