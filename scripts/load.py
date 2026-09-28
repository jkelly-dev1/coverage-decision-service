"""Create the schema and load the generated world into Postgres.

    python3 scripts/load.py

Only the coverage rows that are true at time zero are loaded. The 194 status
changes are NOT: they are applied by the experiment driver as it advances
through the event stream, because coverage.network_status is what the service
reads and it has to hold what is true NOW instead of everything that will
ever be true. Loading them all up front would make the service answer every
early event with a value that had not happened yet, and every staleness figure
in experiment 3 would be measuring the loader.

It refuses to load something else. The manifest is recomputed here and
compared against the `input_manifest_sha256` every shipped results file
carries, before the schema is touched, so a generator that has been edited
cannot silently put a number in results/ that no clone can reproduce. If there
are no results to compare against, as in a tree that has never been run, it
says so and loads anyway, because "nothing to compare" is not "compared and
equal" and the difference belongs in the output rather than in a reader's
assumption.

The same comparison runs offline, with no Docker and no Postgres, in
tests/test_generator_and_scoring.py. This refusal protects the load; the test
protects everybody who only runs the suite.
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

    # ---- the refusal, before anything is written ---------------------------
    shipped = lab.shipped_input_manifests()
    if not shipped:
        print("no shipped results to compare against; THE GENERATOR IS NOT "
              "VERIFIED on this run")
    else:
        disagree = lab.manifest_disagreements(man["sha256"], shipped)
        if disagree:
            print(file=sys.stderr)
            print("REFUSING TO LOAD. The generator produces", file=sys.stderr)
            print("    %s" % man["sha256"], file=sys.stderr)
            print("and these shipped results were measured on something else:",
                  file=sys.stderr)
            for name in disagree:
                print("    results/%s.json  %s" % (name, shipped[name]),
                      file=sys.stderr)
            print(file=sys.stderr)
            print("Loading this world would leave every number in README.md "
                  "describing a world", file=sys.stderr)
            print("no clone can rebuild. Revert the generator, or re-run all "
                  "four experiments", file=sys.stderr)
            print("and update the README from them.", file=sys.stderr)
            return 1
        print("manifest verified against %d shipped results file(s)"
              % len(shipped))

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
