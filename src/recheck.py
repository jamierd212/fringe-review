"""
Read recently collected reviews again, in case the rating has changed since.

Nothing else in this pipeline ever looks at a review twice. Once a URL is in
`seen` it is never fetched again, which is the right default - it is what keeps a
daily sweep to a few hundred requests instead of several thousand - but it means
a publication correcting itself never reaches us.

It also catches our own mistakes, which is how it earned its keep before it was
even finished. Binge Fringe's Slugs review was on the board at two stars against
a headline that said three, and the cause was ours: the cleanup that removes the
company name from their titles took the stars with it, and the rating fell
through to a rule that found "These two star performers" in the blurb. Nothing
would ever have looked at that review again.

Only recent reviews are looked at again, because that is when corrections
happen. A star rating that has stood for a fortnight is not about to move, and
re-reading the whole archive nightly would be a great deal of traffic aimed at
finding almost nothing.
"""

from __future__ import annotations

import sqlite3

from . import db
from .collect import Collector, load_config

# How far back to look. Corrections land within hours; a week is generous and
# still bounds the work to the current feed window.
RECENT_DAYS = 7
# Fetching a page again costs a request per review, so those sources get a
# tighter window than the ones whose rating arrives free in the feed.
RECENT_DAYS_IF_FETCHING = 2
# Above this share of a publication's rechecked reviews changing, we stop and
# report rather than apply. One review moving is a correction; half of them
# moving is our own extraction having broken, and applying that would overwrite
# a shelf of good ratings with a bug.
SUSPICIOUS_SHARE = 0.5


def run(conn: sqlite3.Connection, only: list[str] | None = None,
        apply: bool = True) -> int:
    """Re-rate recent reviews. Returns the number whose rating changed."""
    collector = Collector(load_config())
    if only:
        wanted = {n.casefold() for n in only}
        collector.publications = [p for p in collector.publications
                                  if p["name"].casefold() in wanted]

    total_changed = 0
    for pub in collector.publications:
        window = RECENT_DAYS_IF_FETCHING if pub.get("fetch_page") else RECENT_DAYS
        held = {
            row["url"]: row for row in conn.execute(
                """SELECT url, stars, original, method, headline FROM reviews
                   WHERE publication = ?
                     AND date(first_seen) >= date('now', ?)""",
                (pub["name"], f"-{window} days"))
        }
        if not held:
            continue

        candidates = [c for c in collector.discover(pub, None) if c.url in held]
        if not candidates:
            continue

        changes, checked = [], 0
        for cand in candidates:
            # Rated from the untouched headline, exactly as the sweep now does:
            # the per-publication cleanups can remove the very characters the
            # rating is written in.
            rating = collector.rate(pub, cand)
            if rating is None:
                # Not a disagreement: a page that has stopped yielding a rating
                # tells us nothing about whether the old one was right.
                continue
            checked += 1
            was = held[cand.url]
            if rating.stars != was["stars"]:
                changes.append((cand.url, was["stars"], rating))

        if not checked:
            continue
        share = len(changes) / checked
        if changes and share > SUSPICIOUS_SHARE:
            print(f"  !! {pub['name']}: {len(changes)} of {checked} ratings "
                  f"disagree with what we stored — too many to be corrections, "
                  f"so nothing has been changed. Check the rating rule.")
            for url, before, rating in changes[:3]:
                print(f"       {before}* -> {rating.stars}*  {url[:70]}")
            continue

        for url, before, rating in changes:
            print(f"  {pub['name']}: {before}* -> {rating.stars}*  "
                  f"({rating.method})  {url[:64]}")
            if apply:
                conn.execute(
                    """UPDATE reviews SET stars = ?, original = ?, converted = ?,
                                          rounded = ?, method = ?
                       WHERE url = ?""",
                    (rating.stars, rating.original, int(rating.converted),
                     int(rating.rounded), rating.method, url),
                )
        total_changed += len(changes)

    if apply:
        conn.commit()
    return total_changed
