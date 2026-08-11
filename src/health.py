"""
Which sources have gone quiet, and loudly enough that someone notices.

Every failure so far has been silent. Corr Blimey changed a headline format and
lost a week; Broadway Baby has never collected anything from the runner; Musical
Theatre Review started serving a bot challenge mid-festival. None of it produced
an error, because a publication that stops publishing and a publication we can
no longer read look identical from inside the pipeline.

The distinction this makes is between a source that HAS produced reviews and has
stopped, and one that never produced any at all. Both matter; only the first can
be judged against its own history.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

# A source is judged only against recent history, so a publication that is
# simply between festivals is not reported every day of the winter.
WINDOW_DAYS = 21
# How many gaps of its own average length a source may miss before we call it.
TOLERANCE = 3
# Never complain before three days (publications skip a day, and a quiet
# Wednesday is not a fault) and never stay quiet past ten (a source that has
# genuinely broken should not hide behind a slow average).
MIN_SILENT_DAYS = 3
MAX_SILENT_DAYS = 10


def silent_sources(conn: sqlite3.Connection, publications: list[dict],
                   today: date | None = None) -> list[tuple[str, str]]:
    """[(publication, why)] for enabled sources that should be producing and are not."""
    today = today or date.today()
    enabled = [p["name"] for p in publications if p.get("enabled", True)]

    last: dict[str, date] = {}
    for name, seen in conn.execute(
        "SELECT publication, MAX(date(first_seen)) FROM reviews GROUP BY publication"
    ):
        if seen:
            last[name] = datetime.strptime(seen, "%Y-%m-%d").date()

    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    recent = dict(conn.execute(
        "SELECT publication, COUNT(*) FROM reviews WHERE date(first_seen) >= ? "
        "GROUP BY publication", (since,)))

    out: list[tuple[str, str]] = []
    for name in enabled:
        if name not in last:
            out.append((name, "has never collected a review"))
            continue
        quiet = (today - last[name]).days
        rate = recent.get(name, 0)
        if not rate:
            # Nothing in three weeks and nothing to compare against: this is a
            # publication between festivals, not one that broke this morning.
            continue
        # Each source is judged against its own cadence. WhatsOnStage publishes
        # a review a fortnight; four days of quiet is what it always looks like.
        # Binge Fringe publishes daily, so four days is a fault.
        gap = WINDOW_DAYS / rate
        limit = min(max(TOLERANCE * gap, MIN_SILENT_DAYS), MAX_SILENT_DAYS)
        if quiet > limit:
            out.append((name, f"nothing for {quiet} days (last {last[name]}, "
                              f"usually every {gap:.1f})"))

    # When most sources stop within days of each other, nothing has broken: the
    # festival has ended and there is nothing left to review. Reporting that as
    # fifteen faults every morning through September is how an alert stops being
    # read, and the one real fault hiding among them stops being seen. A fault
    # is a source behaving unlike its peers, so only report the outliers.
    judged = [n for n in enabled if recent.get(n)]
    if judged and len(out) > len(judged) / 2:
        # Said out loud rather than swallowed: if this ever appears in the middle
        # of August, the common cause is ours and the run needs looking at.
        print(f"  {len(out)} of {len(judged)} sources quiet at once — reading "
              f"that as the end of the festival, not {len(out)} faults")
        return [(n, why) for n, why in out if n not in judged]
    return out


def report(conn: sqlite3.Connection, publications: list[dict]) -> int:
    """Print any silent sources as GitHub annotations. Returns how many."""
    quiet = silent_sources(conn, publications)
    if not quiet:
        print("All enabled sources have collected a review recently.")
        return 0
    print(f"{len(quiet)} source(s) not collecting:")
    for name, why in quiet:
        # ::error:: surfaces in the Actions UI; the non-zero exit is what makes
        # GitHub send an email, which is the part that reaches a person.
        print(f"::error title=Source silent::{name} — {why}")
    return len(quiet)
