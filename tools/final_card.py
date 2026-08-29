"""
The final-weekend card: the whole board, in purple.

Same layout as the daily card, with the header saying what this one is and the
page in a different colour so it reads as the closing post rather than another
morning's.

    python tools/final_card.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import card, db, rank, standings        # noqa: E402

YEAR = 2026


def main() -> int:
    conn = db.connect()
    ranked, _rest = rank.leaderboard(conn, YEAR)
    placed = rank.placement(conn, ranked, YEAR)

    previous = standings.positions(conn, YEAR)
    when = date.today()
    image = card.draw_card(placed, when=when, previous=previous,
                           qualifier="- Overall",
                           strapline="Final Weekend Standings",
                           bg=card.BG_PURPLE, totals=rank.totals(conn, YEAR),
                           slug="final20")
    _text, named = card.caption(conn, placed, when=when,
                                heading="Edinburgh Festivals Top 20 — Final Weekend",
                                slug="final-")
    taggable = card.tags(conn, placed, previous,
                         standings.peaks(conn, YEAR, placed), when=when,
                         heading="Tag people (final)", slug="final-")

    print(f"  {len(placed)} shows ranked, "
          f"{sum(len(s.reviews) for _, s in placed):,} reviews behind them")
    print(f"  card: {image.name}  ({named} of 20 with an Instagram handle)")
    print(f"  tags: {taggable} handle(s) to choose from")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
