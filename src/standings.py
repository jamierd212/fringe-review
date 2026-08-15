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
# Bluesky's limit. Kept here because the message is composed to fit it.
LIMIT = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS standings (
    year     INTEGER NOT NULL,
    show_id  TEXT NOT NULL,
    position INTEGER NOT NULL,
    seen_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (year, show_id)
);
"""


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def compose(show, position: int, previous: int) -> str:
    """
    The message, as it will appear.

    `previous` is not named in it. The interesting fact is where the show is now,
    and "up from #12" invites the reader to notice the days it was lower.
    """
    average = f"{show.mean:.1f}".rstrip("0").rstrip(".")

    def build(title: str) -> str:
        return (
            f"Congratulations! {title} is up to #{position} on Fringestars.com "
            f"- the definitive Edinburgh festival reviews aggregator.\n"
            f"{average} Star Rating from {_plural(len(show.reviews), 'review')}.\n"
            f"{SITE}/show/{show.id}/"
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


def update(conn: sqlite3.Connection, year: int, placed, notify: bool = True) -> list[dict]:
    """
    Record today's positions and return the notes for shows that rose.

    `placed` is [(position, show)] as the leaderboard renders it. The first run
    for a year records everything and reports nothing: without a previous
    position there is no move to describe, and congratulating the whole top
    twenty for standing still is not what was asked for.
    """
    conn.executescript(SCHEMA)
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
                    "written_at": datetime.now().isoformat(timespec="seconds"),
                })
        conn.execute(
            """INSERT INTO standings (year, show_id, position, seen_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(year, show_id) DO UPDATE SET
                   position = excluded.position, seen_at = excluded.seen_at""",
            (year, show.id, position),
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
