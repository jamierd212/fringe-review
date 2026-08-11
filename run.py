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

from src import collect, db, health, match, programme, render


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Fringe review leaderboard.")
    parser.add_argument("--backfill", metavar="YYYY-MM",
                        help="read a month's archive instead of the live feed")
    parser.add_argument("--limit", type=int,
                        help="only process N items per publication (quick tests)")
    parser.add_argument("--health", action="store_true",
                        help="report sources that have stopped collecting, and "
                             "exit non-zero if any (so the run is marked failed "
                             "and GitHub emails you)")
    parser.add_argument("--render", action="store_true", help="only rebuild the HTML")
    parser.add_argument("--match", action="store_true", help="only redo show matching")
    parser.add_argument("--rematch", action="store_true",
                        help="discard all show assignments and match again from scratch "
                             "(keeps the collected reviews; use after changing matching)")
    parser.add_argument("--reset", action="store_true", help="wipe the database first")
    args = parser.parse_args()

    if args.reset and db.DB_PATH.exists():
        db.DB_PATH.unlink()
        print(f"Deleted {db.DB_PATH.name}")

    conn = db.connect()

    # A festival's coverage runs from mid-July previews to September stragglers,
    # so a bare year walks all three months rather than August alone.
    months: list[tuple[int, int]] = []
    year = date.today().year
    if args.backfill:
        try:
            parts = args.backfill.split("-")
            year = int(parts[0])
            months = ([(year, int(parts[1]))] if len(parts) > 1
                      else [(year, 7), (year, 8), (year, 9)])
        except (ValueError, IndexError):
            print("--backfill needs YYYY or YYYY-MM, e.g. 2025 or 2025-08",
                  file=sys.stderr)
            return 2

    if args.rematch:
        # Reviews are expensive to collect and cheap to re-match, so throw away
        # only the derived layer: which show each review belongs to.
        conn.execute("UPDATE reviews SET show_id = NULL, confidence = NULL")
        conn.execute("DELETE FROM aliases")
        conn.execute("DELETE FROM shows")
        conn.commit()
        print("Cleared all show assignments; re-matching from scratch.")
        args.match = True

    if args.health:
        cfg = collect.load_config()
        conn.close()
        # Non-zero marks the workflow failed, which is what makes GitHub send an
        # email. The data is already committed by the time this step runs.
        return 1 if health.report(db.connect(), cfg["publications"]) else 0

    if not (args.render or args.match):
        if months:
            for y, m in months:
                print(f"\nCollecting reviews from {y}-{m:02d}\n")
                collect.run(conn, backfill=(y, m), limit=args.limit)
        else:
            print("\nCollecting reviews\n")
            collect.run(conn, backfill=None, limit=args.limit)

    if not args.render:
        print("\nMatching reviews to shows")
        counts = match.run(conn, year)
        print(f"  {counts['exact']} exact, {counts['fuzzy']} fuzzy, "
              f"{counts['new']} new shows, {counts['flagged']} flagged for checking")
        if counts.get("held"):
            print(f"  {counts['held']} held — not on the leaderboard "
                  f"(see review_queue.md)")

    # Link this year's shows to their official programme entries and read the
    # Fringe's own genre/subGenre. Past years are skipped: their programme pages
    # are taken down once the shows stop selling.
    if not args.render:
        current = date.today().year
        counts = programme.enrich(conn, current)
        if counts["matched"] or counts["missed"]:
            print(f"  programme: {counts['matched']} linked, {counts['missed']} not found")

    paths = render.run(conn, year)
    stats = db.stats(conn)
    print("\nWrote " + ", ".join(p.name for p in paths))
    print(f"  {stats['shows']} shows, {stats['rated']} rated reviews, "
          f"{stats['seen']} URLs seen")
    # index.html, not paths[0] — that is the newest year, which before the
    # festival opens is the empty placeholder rather than what visitors see.
    print(f"\nOpen it with:  open {paths[0].parent / 'index.html'}\n")

    if not args.render:
        health.report(conn, collect.load_config()["publications"])

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
