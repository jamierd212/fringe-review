#!/usr/bin/env python3
"""
The one command that does everything.

    python run.py                      # normal daily run
    python run.py --backfill 2025-08   # load real August 2025 reviews, for testing
    python run.py --render             # just rebuild the page from what's stored
    python run.py --match              # just redo show matching (after fixing aliases)
    python run.py --reset              # delete the database and start over
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from src import collect, db, match, render


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Fringe review leaderboard.")
    parser.add_argument("--backfill", metavar="YYYY-MM",
                        help="read a month's archive instead of the live feed")
    parser.add_argument("--limit", type=int,
                        help="only process N items per publication (quick tests)")
    parser.add_argument("--render", action="store_true", help="only rebuild the HTML")
    parser.add_argument("--match", action="store_true", help="only redo show matching")
    parser.add_argument("--reset", action="store_true", help="wipe the database first")
    args = parser.parse_args()

    if args.reset and db.DB_PATH.exists():
        db.DB_PATH.unlink()
        print(f"Deleted {db.DB_PATH.name}")

    conn = db.connect()

    backfill = None
    year = date.today().year
    if args.backfill:
        try:
            y, m = args.backfill.split("-")
            backfill, year = (int(y), int(m)), int(y)
        except ValueError:
            print("--backfill needs the form YYYY-MM, e.g. 2025-08", file=sys.stderr)
            return 2

    if not (args.render or args.match):
        print("\nCollecting reviews" + (f" from {args.backfill}" if backfill else "") + "\n")
        collect.run(conn, backfill=backfill, limit=args.limit)

    if not args.render:
        print("\nMatching reviews to shows")
        counts = match.run(conn, year)
        print(f"  {counts['exact']} exact, {counts['fuzzy']} fuzzy, "
              f"{counts['new']} new shows, {counts['flagged']} flagged for checking")

    path = render.run(conn, year)
    stats = db.stats(conn)
    print(f"\nWrote {path}")
    print(f"  {stats['shows']} shows, {stats['rated']} rated reviews, "
          f"{stats['seen']} URLs seen")
    print(f"\nOpen it with:  open {path}\n")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
