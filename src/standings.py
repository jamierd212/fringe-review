"""
Notice when a show climbs the leaderboard, and write a note to send about it.

Nothing here posts anything. It records where every show stood at the end of a
run, works out which ones rose since the last one, and writes the messages to an
outbox for a person to read and send. Posting is a separate, deliberate act:
a public message cannot be recalled, and a run that misbehaves at three in the
morning should not be able to publish before anyone has seen it.

Only rises are reported, and only inside the top twenty. A show that falls is
told nothing, which is the whole point of the exercise.

Ties make "moved up a place" less obvious than it sounds. Four shows share
fourteenth place today; if one of them is overtaken it keeps the number fourteen
while genuinely having been passed. So the test is the position NUMBER falling
for that show, which fires once per real move and never for a move somebody else
made.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

TOP = 20
OUTBOX = Path(__file__).resolve().parent.parent / "data" / "outbox.json"
SITE = "https://www.fringestars.com"
# What the link reads as. The scheme and the www are noise in a congratulation,
# and the facet carries the real address, so the text can be the readable form.
SITE_SHOWN = "fringestars.com"
# Bluesky's limit. Kept here because the message is composed to fit it.
LIMIT = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS standings (
    year     INTEGER NOT NULL,
    show_id  TEXT NOT NULL,
    position INTEGER NOT NULL,
    best     INTEGER,
    seen_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (year, show_id)
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """
    Add `best` to a standings table written before it existed.

    Kept although nothing reads it yet: it was added to decide who to tag, and
    tagging is gone, but a show's high-water mark is a real fact that costs one
    column to keep and cannot be reconstructed later if it is not recorded now.
    """
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(standings)")}
    if "best" not in columns:
        conn.execute("ALTER TABLE standings ADD COLUMN best INTEGER")
        conn.execute("UPDATE standings SET best = position WHERE best IS NULL")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def compose(show, position: int, previous: int) -> str:
    """
    The message, as it will appear.

    `previous` is not named in it. The interesting fact is where the show is now,
    and "up from #12" invites the reader to notice the days it was lower.
    """
    # One decimal always: "5 Star Rating" beside "4.9 Star Rating" reads as a
    # different kind of measurement rather than the top of the same one.
    average = f"{show.mean:.1f}"

    def build(title: str) -> str:
        return (
            f"Congratulations! {title} is up to #{position} on fringestars.com "
            f"- the definitive Edinburgh festival reviews aggregator.\n"
            f"{average} Star Rating from {_plural(len(show.reviews), 'review')}.\n"
            f"{SITE_SHOWN}/show/{show.id}/"
        )

    text = build(show.title)
    if len(text) > LIMIT:
        # Shorten the title rather than drop the message. Fringe titles run long
        # — "Man Sings The Same Song Over And Over Again For An Hour" — and the
        # longest already in the top twenty lands at 279 of the 300 allowed, so
        # this is a matter of when rather than whether.
        room = len(show.title) - (len(text) - LIMIT) - 1
        text = build(show.title[:max(room, 12)].rstrip() + "\u2026")
    return text


def positions(conn: sqlite3.Connection, year: int) -> dict[str, int]:
    """
    Where every show stood at the end of the last run.

    Read before update() overwrites it. The card marks the shows that have
    climbed, and it is drawn after the standings are recorded, so by then
    "yesterday" has already become "today" unless somebody kept a copy.
    """
    _migrate(conn)
    return {row[0]: row[1] for row in conn.execute(
        "SELECT show_id, position FROM standings WHERE year = ?", (year,))}


def peaks(conn: sqlite3.Connection, year: int, placed) -> set[str]:
    """
    Shows that have just gone higher than they have ever been.

    Read before update() records today. A show that sat at 5, slipped to 10 and
    climbed back to 6 has no news: it has been higher. Reaching 4 is news.

    This is the whole tagging list. Instagram has no bulk tagging — each handle
    is tapped onto the photo and typed by hand — so a list of fifteen is five
    minutes of fiddling every morning and a list of three is under a minute.
    Restricting it to real highs is what makes the job small enough to do.
    """
    _migrate(conn)
    best = {row[0]: row[1] for row in conn.execute(
        "SELECT show_id, best FROM standings WHERE year = ? AND best IS NOT NULL",
        (year,))}
    return {show.id for position, show in placed
            if best.get(show.id) is None or position < best[show.id]}


def update(conn: sqlite3.Connection, year: int, placed, notify: bool = True) -> list[dict]:
    """
    Record today's positions and return the notes for shows that rose.

    `placed` is [(position, show)] as the leaderboard renders it. The first run
    for a year records everything and reports nothing: without a previous
    position there is no move to describe, and congratulating the whole top
    twenty for standing still is not what was asked for.
    """
    _migrate(conn)
    before = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT show_id, position FROM standings WHERE year = ?", (year,))
    }

    notes: list[dict] = []
    for position, show in placed:
        if notify and before and position <= TOP:
            previous = before.get(show.id)
            if previous is not None and position < previous:
                notes.append({
                    "show_id": show.id,
                    "title": show.title,
                    "position": position,
                    "previous": previous,
                    "text": compose(show, position, previous),
                    # The address the link goes to, kept beside the text it is
                    # shown as, so the sender does not have to reconstruct it.
                    "url": f"{SITE}/show/{show.id}/",
                    "written_at": datetime.now().isoformat(timespec="seconds"),
                })
        conn.execute(
            """INSERT INTO standings (year, show_id, position, best, seen_at)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(year, show_id) DO UPDATE SET
                   position = excluded.position,
                   best = MIN(COALESCE(standings.best, excluded.position),
                              excluded.position),
                   seen_at = excluded.seen_at""",
            (year, show.id, position, position),
        )
    conn.commit()
    return notes


def queue(notes: list[dict]) -> int:
    """Add notes to the outbox, skipping any already waiting for the same show."""
    if not notes:
        return 0
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    waiting = []
    if OUTBOX.exists():
        try:
            waiting = json.loads(OUTBOX.read_text())
        except ValueError:
            waiting = []
    # One pending note per show: a show climbing three days running should be
    # congratulated once, on the best position it reached, not three times.
    by_show = {n["show_id"]: n for n in waiting}
    added = 0
    for note in notes:
        current = by_show.get(note["show_id"])
        if current is None or note["position"] < current["position"]:
            if current is None:
                added += 1
            else:
                note["previous"] = current["previous"]   # keep the original start
            by_show[note["show_id"]] = note
    OUTBOX.write_text(json.dumps(sorted(by_show.values(),
                                        key=lambda n: n["position"]), indent=2))
    return added
