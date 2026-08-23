"""
The comedy twenty: card, caption and tag list.

The same board narrowed to what the festival files as comedy, ranked within
itself so it reads 1-20 rather than showing the gaps where theatre used to be.

Standings are kept under their own board name, so the arrows on this card
measure against the last comedy card and not against the whole leaderboard.
The first run records positions and marks nothing, which is honest: there is
nothing yet to have moved from.

    python tools/comedy_card.py
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import card, db, rank, standings        # noqa: E402

BOARD = "comedy"
YEAR = 2026

# The festival files a show under one genre and up to two subgenres. Stand-up is
# in COMEDY, but a great deal of comedy is filed as THEATRE, CABARET or OPERA
# with comedy named underneath — dark comedy, musical comedy, character comedy.
# Taking only the section drops Bog Witch, Linus Karp and Trans People Are
# Awful, all of which are in the comedy top thirteen.
#
# A musical that lists comedy second is a different thing: EVITA TOO is filed
# "Musical theatre,Comedy" and belongs on the main board, not at number four of
# the comedy twenty. Excluded by naming musical theatre rather than by demanding
# comedy come first, which would also have thrown out Linus Karp, The Pitch and
# The Bob Ross Effect for listing another tag ahead of it. Musical COMEDY stays:
# that is a kind of comedy, not a musical with jokes in.
SELECT = """
    SELECT id FROM shows
     WHERE year = ?
       AND (UPPER(COALESCE(genre, '')) = 'COMEDY'
            OR LOWER(COALESCE(subgenre, '')) LIKE '%comedy%')
       AND LOWER(COALESCE(subgenre, '')) NOT LIKE '%musical theatre%'
"""


def main() -> int:
    conn = db.connect()
    conn.row_factory = sqlite3.Row

    comedy = {r["id"] for r in conn.execute(SELECT, (YEAR,))}
    ranked, _rest = rank.leaderboard(conn, YEAR)
    only = [s for s in ranked if s.id in comedy]
    placed = rank.strict_positions(only, rank.scarcity(conn, YEAR))

    # Read before update() overwrites them, as the main card does.
    previous = standings.positions(conn, YEAR, board=BOARD)
    highs = standings.peaks(conn, YEAR, placed, board=BOARD)

    # Safe to run repeatedly: standings keep what today's recording replaced, so
    # a redraw still measures against the last card rather than against itself.
    standings.update(conn, YEAR, placed, notify=False, board=BOARD)

    when = date.today()
    image = card.draw_card(placed, when=when, previous=previous,
                           qualifier="- Comedy", bg=card.BG_GREEN,
                           slug="comedy20")
    _text, named = card.caption(conn, placed, when=when,
                                heading="Edinburgh Festivals Comedy Top 20",
                                slug="comedy-")
    taggable = card.tags(conn, placed, previous, highs, when=when,
                         heading="Tag people (comedy)", slug="comedy-")

    print(f"  {len(only)} comedy show(s) with reviews, of {len(ranked)} on the board")
    print(f"  card:    {image.name}  ({named} of 20 with an Instagram handle)")
    print(f"  tags:    {taggable} handle(s) to choose from")
    if not previous:
        print("  arrows:  none — this is the first comedy card, so nothing has moved yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
