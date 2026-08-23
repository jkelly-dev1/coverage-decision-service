"""Create the schema and load the generated world into Postgres.

    python3 scripts/load.py

Only the coverage rows that are true at time zero are loaded. The 194 status
changes are NOT: they are applied by the experiment driver as it advances
through the event stream, because coverage.network_status is what the service
reads and it has to hold what is true NOW rather than everything that will
ever be true. Loading them all up front would make the service answer every
early event with a value that had not happened yet, and every staleness figure
in experiment 3 would be measuring the loader.

Refuses rather than loading something else. The manifest is recomputed here
and compared against what the shipped results were measured on, so a generator
that has been edited cannot quietly put a number in results/ that no clone can
reproduce.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import generate_traffic as gen
import lab

SCHEMA = os.path.join(lab.REPO, "sql", "schema.sql")


def main():
    print("generating ...", end=" ", flush=True)
    mem, mer, status, stream, distinct = gen.build()
    man = gen.manifest(mem, mer, status, stream, distinct)
    print("ok")

    print("schema ...", end=" ", flush=True)
    lab.psql_file(SCHEMA)
    print("ok")

    initial = [r for r in status if r["effective_from"] == 0]
    pending = [r for r in status if r["effective_from"] > 0]

    print(lab.copy_rows("coverage.member", gen.MEMBER_COLS, mem))
    print(lab.copy_rows("coverage.merchant", gen.MERCHANT_COLS, mer))
    print(lab.copy_rows("coverage.network_status", gen.STATUS_COLS, initial))
    print(lab.copy_rows("truth.event", gen.EVENT_COLS, distinct))
    print(lab.copy_rows("truth.arrival", gen.ARRIVAL_COLS, stream))

    lab.psql("ANALYZE coverage.member; ANALYZE coverage.merchant;"
             " ANALYZE coverage.network_status; ANALYZE truth.event;"
             " ANALYZE truth.arrival;")

    # Asserted, not printed and trusted. A \copy that loaded 19,999 of 20,000
    # rows exits 0 and prints the count in a line that is easy to miss.
    counts = {
        "coverage.member": len(mem),
        "coverage.merchant": len(mer),
        "coverage.network_status": len(initial),
        "truth.event": len(distinct),
        "truth.arrival": len(stream),
    }
    bad = []
    for table, expected in counts.items():
        got = int(lab.scalar("SELECT count(*) FROM %s;" % table))
        if got != expected:
            bad.append("%s: loaded %d of %d" % (table, got, expected))

    print()
    for table, expected in sorted(counts.items()):
        print("%-26s %7d" % (table, expected))
    print("%-26s %7d  (applied by the driver, not loaded here)"
          % ("status changes pending", len(pending)))
    print("manifest %s" % man["sha256"])
    if bad:
        for line in bad:
            print("LOAD IS SHORT. " + line, file=sys.stderr)
        return 1
    print("load verified against the generator")
    return 0


if __name__ == "__main__":
    sys.exit(main())
