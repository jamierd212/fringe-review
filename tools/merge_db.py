"""
Combine two copies of the database after both have collected.

The database is committed, so whenever the scheduled sweep and anything else
both write to it, git is left with two versions of a binary file and no way to
reconcile them. Rebasing cannot work — there is no textual diff to replay — and
taking either side throws away a day's collecting.

Every table in here is an accumulating record keyed on something stable, so the
answer is neither side but both: insert the rows we are missing.

    python tools/merge_db.py data/reviews.db other.db

INSERT OR IGNORE rather than REPLACE, so where both hold the same key the copy
being merged INTO wins. Called from the daily workflow, that is the sweep's own
freshly collected row rather than an older one arriving from the remote.
"""

from __future__ import annotations

import sqlite3
import sys


def keyless(conn: sqlite3.Connection, table: str) -> bool:
    """True when nothing in the table is a primary key, so rows can duplicate."""
    return not any(row[5] for row in conn.execute(f"PRAGMA table_info({table})"))


def merge(mine: str, theirs: str) -> int:
    conn = sqlite3.connect(mine)
    conn.execute("ATTACH ? AS theirs", (theirs,))
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]

    total = 0
    for table in sorted(tables):
        ours = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        thrs = {r[1] for r in conn.execute(f"PRAGMA theirs.table_info({table})")}
        shared = sorted(ours & thrs)
        if not shared:
            print(f"  {table}: no columns in common, skipped")
            continue
        cols = ", ".join(f'"{c}"' for c in shared)

        before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        conn.execute(f"INSERT OR IGNORE INTO main.{table} ({cols}) "
                     f"SELECT {cols} FROM theirs.{table}")

        # A table with no key at all cannot refuse a duplicate, so the union
        # doubles every row the other side held. source_probes is the one: an
        # append-only log of which sources answered. Identical rows are the same
        # event recorded twice.
        if keyless(conn, table):
            conn.execute(
                f"DELETE FROM {table} WHERE rowid NOT IN "
                f"(SELECT MIN(rowid) FROM {table} GROUP BY {cols})")

        after = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if after != before:
            print(f"  {table}: +{after - before}")
            total += after - before

    conn.commit()
    ok = conn.execute("PRAGMA quick_check").fetchone()[0]
    if ok != "ok":
        print(f"  integrity check failed: {ok}", file=sys.stderr)
        return -1
    print(f"  {total} row(s) taken from {theirs}")
    return total


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(0 if merge(sys.argv[1], sys.argv[2]) >= 0 else 1)
