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
from pathlib import Path

from rapidfuzz import fuzz, process

from .normalise import alias_forms, clean_title, normalise, split_performer

ACCEPT = 92      # fuzzy score at or above this is taken automatically
CONSIDER = 74    # between CONSIDER and ACCEPT: record it, but flag for a human

QUEUE_PATH = Path(__file__).resolve().parent.parent / "review_queue.md"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalise(text)).strip("-")[:80] or "unknown"


def run(conn: sqlite3.Connection, year: int) -> dict[str, int]:
    unmatched = conn.execute(
        "SELECT url, headline, publication FROM reviews WHERE show_id IS NULL"
    ).fetchall()

    counts = {"exact": 0, "fuzzy": 0, "new": 0, "flagged": 0}
    flagged: list[tuple] = []

    for row in unmatched:
        headline = row["headline"] or ""
        aliases = alias_forms(headline)
        if not aliases:
            continue

        show_id, confidence, how = _resolve(conn, aliases, headline, year)
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
    return counts


def _resolve(conn, aliases: list[str], headline: str, year: int) -> tuple[str, float, str]:
    """Return (show_id, confidence, method)."""

    # 1. Exact hit on a spelling we already know.
    placeholders = ",".join("?" * len(aliases))
    hit = conn.execute(
        f"SELECT show_id FROM aliases WHERE alias IN ({placeholders}) LIMIT 1", aliases
    ).fetchone()
    if hit:
        return hit["show_id"], 1.0, "exact"

    # 2. Fuzzy against every alias we know about.
    known = conn.execute("SELECT alias, show_id FROM aliases").fetchall()
    if known:
        lookup = {r["alias"]: r["show_id"] for r in known}
        best_id, best_score = None, 0.0
        for alias in aliases:
            match = process.extractOne(
                alias, lookup.keys(), scorer=fuzz.token_sort_ratio, score_cutoff=CONSIDER
            )
            if match and match[1] > best_score:
                best_score = match[1]
                best_id = lookup[match[0]]

        if best_id and best_score >= ACCEPT:
            return best_id, best_score / 100.0, "fuzzy"
        if best_id and best_score >= CONSIDER:
            # Plausible but not certain. Create a NEW show rather than risk a
            # false merge, and let the queue tell a human to link them.
            return _create(conn, headline, year), best_score / 100.0, "new"

    # 3. Genuinely new.
    return _create(conn, headline, year), 1.0, "new"


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
