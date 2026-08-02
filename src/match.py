"""
Stage 3: decide which show each review is about.

This is the stage that decides whether the leaderboard looks professional or
broken. Two failure modes, both visible to users:

  false merge  - two different shows collapsed into one row
  false split  - one show appearing as three separate rows

We bias towards false splits. A duplicate row is embarrassing; a merged row
silently attributes someone else's five-star review to the wrong show, which is
worse. Anything we are not confident about goes to review_queue.md for a human.
"""

from __future__ import annotations

import re
import sqlite3
from email.utils import parsedate_to_datetime
from pathlib import Path

from rapidfuzz import fuzz, process

from .ai_match import Matcher
from .normalise import alias_forms, clean_title, normalise, split_performer

ACCEPT = 92      # fuzzy score at or above this is taken automatically
CONSIDER = 74    # between CONSIDER and ACCEPT: ambiguous — ask the AI
SHORTLIST = 5    # how many candidates to put in front of the model

QUEUE_PATH = Path(__file__).resolve().parent.parent / "review_queue.md"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalise(text)).strip("-")[:80] or "unknown"


def festival_year(published: str | None, fallback: int) -> int:
    """
    Which festival a review belongs to, taken from its own publication date.

    Deriving this from the run mode instead — "we passed --backfill 2025-08, so
    everything in this run is 2025" — is what corrupted the first database: a
    later `--match` with no flag defaulted to the current year and silently
    relabelled every 2025 show as 2026. The review's own date cannot drift like
    that.

    Calendar year is the right granularity: the Fringe runs in August, and
    reviews trickle in from late July to early September without ever crossing
    a year boundary.
    """
    if published:
        try:
            return parsedate_to_datetime(published).year
        except (TypeError, ValueError):
            pass
    return fallback


def run(conn: sqlite3.Connection, year: int) -> dict[str, int]:
    unmatched = conn.execute(
        "SELECT url, headline, publication, published FROM reviews WHERE show_id IS NULL"
    ).fetchall()

    counts = {"exact": 0, "fuzzy": 0, "ai": 0, "new": 0, "flagged": 0}
    flagged: list[tuple] = []
    matcher = Matcher(conn)

    for row in unmatched:
        headline = row["headline"] or ""
        aliases = alias_forms(headline)
        if not aliases:
            continue

        # `year` is only a fallback for reviews whose feed gave no usable date.
        review_year = festival_year(row["published"], year)
        show_id, confidence, how = _resolve(
            conn, aliases, headline, review_year, matcher
        )
        counts[how] = counts.get(how, 0) + 1

        conn.execute(
            "UPDATE reviews SET show_id = ?, confidence = ? WHERE url = ?",
            (show_id, confidence, row["url"]),
        )

        # Teach the database this spelling so the next publication's wording lands.
        for alias in aliases:
            conn.execute(
                "INSERT OR IGNORE INTO aliases (alias, show_id) VALUES (?, ?)",
                (alias, show_id),
            )

        if confidence < 0.99:
            title = conn.execute(
                "SELECT title FROM shows WHERE id = ?", (show_id,)
            ).fetchone()["title"]
            flagged.append((row["publication"], headline, title, confidence, row["url"]))
            counts["flagged"] += 1

    conn.commit()
    _write_queue(flagged)
    counts["ai_report"] = matcher.report()
    return counts


def _resolve(conn, aliases: list[str], headline: str, year: int,
             matcher: Matcher) -> tuple[str, float, str]:
    """Return (show_id, confidence, method)."""

    # Every lookup is scoped to the same festival year. A show that returns in a
    # later year is a different run with different reviews, and merging the two
    # would silently pool this year's ratings with last year's.

    # 1. Exact hit on a spelling we already know.
    placeholders = ",".join("?" * len(aliases))
    hit = conn.execute(
        f"""SELECT a.show_id FROM aliases a
              JOIN shows s ON s.id = a.show_id
             WHERE a.alias IN ({placeholders}) AND s.year = ? LIMIT 1""",
        (*aliases, year),
    ).fetchone()
    if hit:
        return hit["show_id"], 1.0, "exact"

    # 2. Fuzzy against every alias from the same year.
    known = conn.execute(
        """SELECT a.alias, a.show_id FROM aliases a
             JOIN shows s ON s.id = a.show_id
            WHERE s.year = ?""",
        (year,),
    ).fetchall()
    if known:
        lookup = {r["alias"]: r["show_id"] for r in known}
        scored: dict[str, float] = {}
        for alias in aliases:
            for candidate, score, _ in process.extract(
                alias, lookup.keys(), scorer=fuzz.token_sort_ratio,
                score_cutoff=CONSIDER, limit=SHORTLIST,
            ):
                scored[candidate] = max(scored.get(candidate, 0.0), score)

        ranked = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)

        if ranked and ranked[0][1] >= ACCEPT:
            return lookup[ranked[0][0]], ranked[0][1] / 100.0, "fuzzy"

        # 3. Ambiguous band. Fuzzy matching can't separate a dropped subtitle
        #    from a genuinely different production, so hand the shortlist to the
        #    model. It answers 0 when unsure, which falls through to "new".
        if ranked:
            # Several aliases can point at shows with identical titles. Offering
            # the model the same title twice is confusing and wastes a call, so
            # keep only the best-scoring alias per distinct title.
            shortlist, titles, seen = [], [], set()
            for alias, _score in ranked:
                title = _title_for(conn, lookup[alias])
                if title.casefold() in seen:
                    continue
                seen.add(title.casefold())
                shortlist.append(alias)
                titles.append(title)
                if len(shortlist) == SHORTLIST:
                    break

            decision = matcher.choose(headline, titles)
            if decision and decision.choice > 0:
                chosen = lookup[shortlist[decision.choice - 1]]
                return chosen, decision.confidence, "ai"

            # Either the model declined or AI matching is off. Create a new show
            # rather than risk a false merge, and flag it for a human.
            return _create(conn, headline, year), ranked[0][1] / 100.0, "new"

    # 4. Genuinely new.
    return _create(conn, headline, year), 1.0, "new"


def _title_for(conn, show_id: str) -> str:
    row = conn.execute("SELECT title FROM shows WHERE id = ?", (show_id,)).fetchone()
    return row["title"] if row else show_id


def _create(conn, headline: str, year: int) -> str:
    performer, title = split_performer(headline)
    title = title or clean_title(headline)
    show_id = f"{year}-{slugify(headline)}"
    conn.execute(
        """INSERT INTO shows (id, title, performer, festival, year)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(id) DO NOTHING""",
        (show_id, title, performer, "fringe", year),
    )
    return show_id


def _write_queue(flagged: list[tuple]) -> None:
    """
    A plain markdown file you skim over coffee. Anything listed here was matched
    with less than full confidence — usually it is right, occasionally two rows
    are the same show and need merging by hand.
    """
    lines = [
        "# Review queue",
        "",
        "Matches the robot was not fully confident about. Check each one; if two",
        "entries are the same show, add the wrong spelling to the `aliases` table",
        "pointing at the right `show_id` and re-run `python run.py --match`.",
        "",
    ]
    if not flagged:
        lines.append("_Nothing to check. \N{PARTY POPPER}_")
    else:
        lines.append("| Publication | Headline | Matched to | Confidence |")
        lines.append("|---|---|---|---|")
        for pub, headline, title, conf, url in sorted(flagged, key=lambda r: r[3]):
            lines.append(
                f"| {pub} | [{headline}]({url}) | {title} | {conf:.0%} |"
            )
    QUEUE_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
