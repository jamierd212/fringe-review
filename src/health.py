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
    """
    Print faults as GitHub annotations. Returns how many are NEW since last run.

    Only a new fault fails the run, because only a new fault is news. The first
    version failed on the standing state instead, so Broadway Baby - which has
    never collected anything and may never be allowed to - failed every sweep
    from the moment it was added. An alert that fires every morning for a known
    condition is one you learn to close without reading, which is the failure
    this whole check exists to prevent.

    A known fault is still printed, as a warning, so it stays visible in the run
    without crying wolf. If it clears and comes back, it is news again.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS health_state (
            problem TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT)""")

    quiet = silent_sources(conn, publications)
    dead = unreachable_sources(conn, publications)
    # A source already reported as unreachable does not also need reporting as
    # silent: the silence is the symptom and the failed fetch is the cause.
    named = {n for n, _ in dead}
    quiet = [(n, w) for n, w in quiet if n not in named]
    broken = (check_links(conn, collector, publications)
              if collector is not None else [])
    # Keyed on the kind of fault and the source, not the wording: "nothing for
    # 4 days" becomes "nothing for 5 days" tomorrow, and that is the same fault.
    current = {f"silent:{n}": ("Source silent", n, why) for n, why in quiet}
    current.update({f"dead:{n}": ("Source unreachable", n, why) for n, why in dead})
    current.update({f"links:{n}": ("Links broken", n, why) for n, why in broken})

    known = {row[0] for row in conn.execute("SELECT problem FROM health_state")}
    # Judged per publication, not per problem. Broadway Baby moving from
    # "silent" to "unreachable" is the same fault described better, and keying
    # on the description alone announced it as newly broken and simultaneously
    # as resolved.
    known_pubs = {k.split(":", 1)[1] for k in known}
    fresh = [k for k in current if k.split(":", 1)[1] not in known_pubs]

    for key, (title, name, why) in current.items():
        # ::error:: with a non-zero exit is what makes GitHub send an email;
        # ::warning:: shows in the run without marking it failed.
        level = "error" if key in fresh else "warning"
        print(f"::{level} title={title}::{name} — {why}")

    current_pubs = {k.split(":", 1)[1] for k in current}
    for name in sorted(known_pubs - current_pubs):
        print(f"Resolved since the last run: {name}")

    now = datetime.now().isoformat(timespec="seconds")
    for key in current:
        conn.execute("INSERT INTO health_state (problem, first_seen, last_seen) "
                     "VALUES (?, ?, ?) ON CONFLICT(problem) DO UPDATE SET "
                     "last_seen=excluded.last_seen", (key, now, now))
    # Forgotten once it clears, so the same fault returning counts as news.
    conn.executemany("DELETE FROM health_state WHERE problem = ?",
                     [(k,) for k in known - set(current)])
    conn.commit()

    if not current:
        print("All enabled sources are collecting, and the links checked "
              "today lead to the review.")
    elif not fresh:
        print(f"\n{len(current)} known problem(s), none new since the last run.")
    return len(fresh)


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


def check_links(conn: sqlite3.Connection, collector, publications: list[dict],
                per_publication: int = 2) -> list[tuple[str, str]]:
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

    # Publications whose pages render nothing server-side cannot be checked this
    # way, and reporting them is worse than not checking: EdFringeReview's review
    # pages are a 3.8KB shell with the text loaded afterwards from Firestore,
    # which is exactly why it is collected through that API. Looking for the show
    # name in the HTML will never find it, so every sample "led nowhere" and the
    # sweep failed every run on a fault that did not exist.
    unreadable = {p["name"] for p in publications if p.get("api")}

    rows = conn.execute("""
        SELECT r.url, r.publication, r.headline
        FROM reviews r LEFT JOIN link_checks c ON c.url = r.url
        WHERE r.stars IS NOT NULL
        ORDER BY c.checked_at IS NOT NULL, c.checked_at, r.first_seen DESC""")

    picked: dict[str, list] = {}
    for url, pub, headline in rows:
        if pub in unreadable:
            continue
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


def unreachable_sources(conn: sqlite3.Connection, publications: list[dict],
                        runs: int = 3) -> list[tuple[str, str]]:
    """
    Sources whose discovery has come back empty on every recent run.

    This is a stronger signal than silence, and an earlier one. "No reviews for
    four days" is inference - a publication may simply not have posted. "The
    last three runs fetched the index and got nothing" is a fact about our own
    requests, and it separates a quiet week from a site that is refusing us or
    has fallen over.

    ThreeWeeks stopped answering mid-festival and would have waited a day for the
    silence rule to notice, having already failed three runs in a row by then.

    A status of None means the connection never completed, which reads like an
    outage and is not necessarily one: ThreeWeeks drops the TLS handshake from
    every command-line client while serving browsers normally from the same
    machine and the same network. The wording says both, because from here the
    two are indistinguishable.
    """
    enabled = {p["name"] for p in publications if p.get("enabled", True)}
    out = []
    for name in sorted(enabled):
        probes = conn.execute(
            """SELECT status, found FROM source_probes WHERE publication = ?
               ORDER BY ran_at DESC LIMIT ?""", (name, runs)).fetchall()
        if len(probes) < runs or any(p[1] for p in probes):
            continue
        statuses = {p[0] for p in probes}
        # One status repeated is the useful case: 403 every time is a block,
        # nothing every time is a connection that never completed.
        if statuses == {None}:
            why = (f"{runs} runs, the connection never completed — either the "
                   f"site is down or it is refusing this client")
        elif len(statuses) == 1:
            code = statuses.pop()
            why = f"{runs} runs, HTTP {code} every time"
        else:
            why = f"{runs} runs, nothing found ({sorted(str(s) for s in statuses)})"
        out.append((name, why))
    return out
