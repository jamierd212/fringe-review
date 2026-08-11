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

import re
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


def report(conn: sqlite3.Connection, publications: list[dict],
           collector=None) -> int:
    """Print faults as GitHub annotations. Returns how many were found."""
    quiet = silent_sources(conn, publications)
    broken = check_links(conn, collector) if collector is not None else []

    for name, why in quiet:
        # ::error:: surfaces in the Actions UI; the non-zero exit is what makes
        # GitHub send an email, which is the part that reaches a person.
        print(f"::error title=Source silent::{name} — {why}")
    for name, why in broken:
        print(f"::error title=Links broken::{name} — {why}")

    if not quiet and not broken:
        print("All enabled sources are collecting, and the links checked "
              "today lead to the review.")
    return len(quiet) + len(broken)


# ---------------------------------------------------------------------------
# Do our links still lead to the review?

# Words this short carry no evidence, and words this common appear on every page
# of a site's furniture, so neither can tell an article from an empty shell.
_STOP = {"review", "edinburgh", "fringe", "festival", "stars", "with", "from",
         "that", "this", "have", "about", "their", "there", "which", "show"}


def _evidence(headline: str) -> list[str]:
    """The words from a headline whose presence would prove the article loaded."""
    words = re.findall(r"[a-z]{5,}", (headline or "").lower())
    return [w for w in dict.fromkeys(words) if w not in _STOP][:4]


def _page_text(html: str) -> str:
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", html).lower()


def check_links(conn: sqlite3.Connection, collector, per_publication: int = 2
                ) -> list[tuple[str, str]]:
    """
    Fetch a few published links per publication and confirm the review is there.

    Checked through the collector so this obeys the same robots rules and crawl
    delays as everything else - a link checker is still a crawler.

    Least-recently-checked first, so a couple of requests a day per publication
    walks the whole site over time rather than hammering it at once.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS link_checks (
            url TEXT PRIMARY KEY, checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
            ok INTEGER, detail TEXT)""")

    rows = conn.execute("""
        SELECT r.url, r.publication, r.headline
        FROM reviews r LEFT JOIN link_checks c ON c.url = r.url
        WHERE r.stars IS NOT NULL
        ORDER BY c.checked_at IS NOT NULL, c.checked_at, r.first_seen DESC""")

    picked: dict[str, list] = {}
    for url, pub, headline in rows:
        got = picked.setdefault(pub, [])
        if len(got) < per_publication:
            got.append((url, headline))

    failures: dict[str, list[str]] = {}
    checked: dict[str, int] = {}
    for pub, items in picked.items():
        for url, headline in items:
            # A page robots.txt puts out of bounds tells us nothing about whether
            # the link works, so it is not recorded and does not use up the
            # publication's sample - otherwise a source we are not allowed to
            # check would look permanently healthy.
            if not collector.allowed(url):
                continue
            html = collector._get(url)
            checked[pub] = checked.get(pub, 0) + 1
            status = getattr(collector, "last_status", None)
            if html is None:
                # A refusal is not a broken link: the page may be fine and simply
                # not for us. Only a missing page counts against the link.
                ok, detail = (status != 404), f"HTTP {status}"
            else:
                words = _evidence(headline)
                text = _page_text(html)
                hits = [w for w in words if w in text]
                # Two words is the bar where a headline's own vocabulary stops
                # being something a navigation menu could supply by accident.
                ok = not words or len(hits) >= min(2, len(words))
                detail = "ok" if ok else f"none of {words} on the page"
            conn.execute("INSERT INTO link_checks (url, checked_at, ok, detail) "
                         "VALUES (?, datetime('now'), ?, ?) ON CONFLICT(url) DO "
                         "UPDATE SET checked_at=excluded.checked_at, "
                         "ok=excluded.ok, detail=excluded.detail",
                         (url, int(ok), detail))
            if not ok:
                failures.setdefault(pub, []).append(f"{url} — {detail}")
    conn.commit()

    # One dead link is an article the publication moved or pulled. Every sampled
    # link failing is our URL pattern being wrong, which is the fault worth an
    # email - it is silent, it affects every link from that source, and it is the
    # one that put 70 blank pages on the leaderboard.
    out = []
    for pub, bad in failures.items():
        if len(bad) >= checked.get(pub, 0):
            out.append((pub, f"all {len(bad)} links sampled led nowhere: {bad[0]}"))
        else:
            out.append((pub, f"{len(bad)} of {checked[pub]} links sampled "
                             f"led nowhere: {bad[0]}"))
    return out
