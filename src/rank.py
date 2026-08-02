"""
Stage 4: the leaderboard maths.

Olympic ranking on 4- and 5-star reviews only. One 5-star review outranks any
number of 4-star reviews, exactly as one gold medal outranks any number of
silvers. Lower ratings are collected and displayed for reference but never
affect a show's position.

Ranking on the top two tiers only is deliberate. Ranking on all five tiers
inverts at the bottom: two shows tied on 5s, 4s, 3s and 2s would be separated by
who had MORE one-star reviews, so a panned show would outrank an unpanned one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class ReviewRef:
    publication: str
    url: str
    stars: int
    original: str
    converted: bool
    rounded: bool

    @property
    def note(self) -> str:
        if self.rounded:
            return f"{self.original} rounded down"
        if self.converted:
            return f"{self.original}"
        return ""


@dataclass
class Show:
    id: str
    title: str
    performer: str | None
    url: str | None = None       # official programme entry
    genre: str = ""             # e.g. "Comedy · Sketch"
    reviews: list[ReviewRef] = field(default_factory=list)

    @property
    def counts(self) -> dict[int, int]:
        out = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0}
        for r in self.reviews:
            out[r.stars] += 1
        return out

    def at(self, stars: int) -> list[ReviewRef]:
        """Reviews at a given star level, best-known publications first."""
        return sorted(
            [r for r in self.reviews if r.stars == stars],
            key=lambda r: r.publication.casefold(),
        )

    @property
    def total(self) -> int:
        return len(self.reviews)

    @property
    def ranked(self) -> bool:
        """Shows with no 4- or 5-star reviews are listed separately, not ranked."""
        c = self.counts
        return (c[5] + c[4]) > 0


def rank_key(show: Show) -> tuple:
    """
    Sort key. Python compares tuples left to right, and negating a number turns
    an ascending sort into a descending one.
    """
    c = show.counts
    return (
        -c[5],                      # most 5-star reviews wins outright
        -c[4],                      # then most 4-star
        -show.total,                # then the most widely reviewed
        show.title.casefold(),      # then alphabetical, so the order is stable
    )


def years(conn: sqlite3.Connection) -> list[int]:
    """Festival years we hold rated reviews for, newest first."""
    rows = conn.execute(
        """SELECT DISTINCT s.year FROM shows s
             JOIN reviews r ON r.show_id = s.id
            WHERE r.stars IS NOT NULL AND s.year IS NOT NULL
            ORDER BY s.year DESC"""
    )
    return [r["year"] for r in rows]


def load(conn: sqlite3.Connection, year: int | None = None) -> list[Show]:
    """Read shows and their reviews, keeping one review per publication."""
    shows: dict[str, Show] = {}
    if year is None:
        rows = conn.execute(
            "SELECT id, title, performer, edfringe_url, genre, subgenre FROM shows")
    else:
        rows = conn.execute(
            """SELECT id, title, performer, edfringe_url, genre, subgenre
                 FROM shows WHERE year = ?""", (year,))
    from .programme import label
    for row in rows:
        shows[row["id"]] = Show(
            row["id"], row["title"], row["performer"],
            url=row["edfringe_url"] or None,
            genre=label(row["genre"] or "", row["subgenre"] or ""),
        )

    # One review per publication per show: a re-review should not count twice.
    # `published DESC` keeps the most recent.
    rows = conn.execute(
        """SELECT show_id, publication, url, stars, original, converted, rounded
             FROM reviews
            WHERE stars IS NOT NULL AND show_id IS NOT NULL
            ORDER BY published DESC"""
    )
    claimed: set[tuple[str, str]] = set()
    for r in rows:
        key = (r["show_id"], r["publication"])
        if key in claimed or r["show_id"] not in shows:
            continue
        claimed.add(key)
        shows[r["show_id"]].reviews.append(
            ReviewRef(
                publication=r["publication"],
                url=r["url"],
                stars=r["stars"],
                original=r["original"] or "",
                converted=bool(r["converted"]),
                rounded=bool(r["rounded"]),
            )
        )

    return [s for s in shows.values() if s.reviews]


def leaderboard(conn: sqlite3.Connection,
                year: int | None = None) -> tuple[list[Show], list[Show]]:
    """Returns (ranked shows in order, unranked shows alphabetically)."""
    shows = load(conn, year)
    ranked = sorted([s for s in shows if s.ranked], key=rank_key)
    rest = sorted([s for s in shows if not s.ranked], key=lambda s: s.title.casefold())
    return ranked, rest


def positions(ranked: list[Show]) -> list[tuple[int, Show]]:
    """
    Attach positions, sharing a number between genuinely tied shows
    (two shows on 3x5 and 1x4 are both 2nd, and the next show is 4th).
    """
    out: list[tuple[int, Show]] = []
    last_key, last_pos = None, 0
    for index, show in enumerate(ranked, start=1):
        key = rank_key(show)[:2]        # tied means same 5- and 4-star counts
        if key != last_key:
            last_pos, last_key = index, key
        out.append((last_pos, show))
    return out
