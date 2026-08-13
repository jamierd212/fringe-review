"""
Add reviews by hand, for the ones the crawler cannot reach.

Some publications cannot be collected: Voice Magazine refuses robots.txt, Broadway
Baby answers our requests with a 403, ThreeWeeks drops the connection before any
request is made. A person reading a public page and typing what it says is not
crawling, and it is the only way those reviews reach the board.

Reads lines of  stars | url | headline  from a file or standard input:

    4 | https://example.com/review/thing | Thing: A Show About Things
    3 | https://example.com/review/other | Other Thing

and files each against the right show using the same matcher as everything else,
so a manually added review lands where the crawled ones land. Rows whose URL is
already stored are skipped, so running it twice is harmless.

    python tools/add_reviews.py "ThreeWeeks Edinburgh" reviews.txt
    python tools/add_reviews.py "Broadway Baby" --dry-run < reviews.txt

Stored with method "reported" rather than one of the extraction methods, so it is
always clear which ratings were read by a person rather than taken from a page.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db                                    # noqa: E402
from src.match import _resolve, festival_year         # noqa: E402
from src.ai_match import Matcher                      # noqa: E402
from src.normalise import alias_forms                 # noqa: E402

SPLIT = re.compile(r"\s*[|\t]\s*")


def parse(text: str) -> list[tuple[int, str, str]]:
    """[(stars, url, headline)] from the pasted lines, complaining about bad ones."""
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        bits = SPLIT.split(line, 2)
        if len(bits) != 3:
            print(f"  line {n}: expected 'stars | url | headline', skipped: {line[:60]}")
            continue
        stars, url, headline = bits
        try:
            value = int(round(float(stars)))
        except ValueError:
            print(f"  line {n}: {stars!r} is not a rating, skipped")
            continue
        if not 1 <= value <= 5 or not url.startswith("http"):
            print(f"  line {n}: rating or URL out of range, skipped: {line[:60]}")
            continue
        out.append((value, url, headline.strip()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("publication", help='exactly as it appears on the site, e.g. "Broadway Baby"')
    ap.add_argument("file", nargs="?", help="file of lines; omit to read standard input")
    ap.add_argument("--year", type=int, default=datetime.now().year)
    ap.add_argument("--published", help="ISO date for all of them; defaults to today")
    ap.add_argument("--dry-run", action="store_true",
                    help="say what would happen and change nothing")
    args = ap.parse_args()

    text = Path(args.file).read_text() if args.file else sys.stdin.read()
    rows = parse(text)
    if not rows:
        print("Nothing to add.")
        return 1

    conn = db.connect()
    matcher = Matcher(conn)
    published = args.published or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    added = skipped = unmatched = 0

    for stars, url, headline in rows:
        if conn.execute("SELECT 1 FROM reviews WHERE url = ?", (url,)).fetchone():
            print(f"  already have  {headline[:46]}")
            skipped += 1
            continue

        aliases = alias_forms(headline)
        fallbacks = [a for a in alias_forms(headline, include_performer=True)
                     if a not in aliases]
        # No gatekeeper: a person has read the review and vouched for it, which is
        # a better answer than the model would give, and the whole reason this
        # exists is that the page cannot be fetched to check.
        show_id, confidence, how = _resolve(
            conn, aliases, fallbacks, headline,
            festival_year(published, args.year), matcher, None,
        )
        if show_id is None:
            print(f"  NO SHOW MATCHED  {headline[:44]}  — left for --match to place")
            unmatched += 1
            how = "unmatched"

        print(f"  {stars}*  {how:9} {headline[:44]}")
        if args.dry_run:
            continue

        conn.execute(
            """INSERT INTO reviews (url, show_id, publication, headline, stars,
                                    original, converted, rounded, published,
                                    confidence, method, first_seen)
               VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, 'reported', datetime('now'))""",
            (url, show_id, args.publication, headline, stars, f"{stars}/5",
             published, confidence if show_id else None),
        )
        # So no future sweep goes looking for a page we cannot fetch.
        conn.execute("INSERT OR REPLACE INTO seen (url, outcome) VALUES (?, 'review')",
                     (url,))
        added += 1

    if not args.dry_run:
        conn.commit()
    verb = "would add" if args.dry_run else "added"
    print(f"\n{verb} {added}, skipped {skipped} already held, {unmatched} unmatched")
    if added and not args.dry_run:
        print("Now run:  python run.py --render")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
