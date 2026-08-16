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


# How many reviews a publication needs before we trust its own rate rather than
# the site-wide one. A source three reviews old, all of them five stars, is not
# evidence that it never gives five stars away.
SELECTIVITY_PRIOR = 20


def selectivity(conn: sqlite3.Connection, year: int | None = None) -> dict[tuple[str, int], float]:
    """
    {(publication, level): how often that publication awards AT LEAST that level}.

    Publications differ enormously in how freely they give stars. On the 2025
    data Chortle awarded five stars to 1% of the shows it reviewed and North
    West End to 33% — so the same five stars mean very different things, and a
    show's reviews read better with the scarcest first.

    "At least the level" rather than "exactly", because it has to work at every
    level: for a four-star review what matters is how often that publication
    goes that high at all, not how often it goes higher.

    Rates are shrunk toward the site-wide rate so a publication with a handful
    of reviews does not look maximally selective on no evidence.
    """
    rows = conn.execute(
        """SELECT r.publication, r.stars FROM reviews r
             JOIN shows s ON s.id = r.show_id
            WHERE r.stars IS NOT NULL AND (? IS NULL OR s.year = ?)""",
        (year, year),
    ).fetchall()

    totals: dict[str, int] = {}
    hits: dict[tuple[str, int], int] = {}
    overall: dict[int, int] = {}
    for pub, stars in rows:
        totals[pub] = totals.get(pub, 0) + 1
        for level in range(1, 6):
            if stars >= level:
                hits[(pub, level)] = hits.get((pub, level), 0) + 1
                overall[level] = overall.get(level, 0) + 1

    n = len(rows) or 1
    out: dict[tuple[str, int], float] = {}
    for pub, total in totals.items():
        for level in range(1, 6):
            site = overall.get(level, 0) / n
            got = hits.get((pub, level), 0)
            out[(pub, level)] = (got + SELECTIVITY_PRIOR * site) / (total + SELECTIVITY_PRIOR)
    return out


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
    # The company as the programme credits it, which is not the same thing:
    # `performer` is whatever a headline called them, this is the billing.
    presented_by: str = ""
    url: str | None = None       # official programme entry
    genre: str = ""             # the badge text, e.g. "Comedy · Sketch"
    # The programme's own values behind that badge. Kept separately because the
    # badge is one readable string while filtering needs the parts: a section to
    # group by and tags to scope to it.
    festival: str = ""          # "fringe", "eif", "freefringe"
    section: str = ""           # e.g. "COMEDY"
    subgenre: str = ""          # e.g. "Stand-up,Solo show"
    venue: str = ""
    start_time: str = ""        # "HH:MM" from the festival's own programme

    @property
    def start_minutes(self) -> int:
        """Start time as minutes past midnight, or -1 when unknown."""
        try:
            hh, mm = self.start_time.split(":")
            return int(hh) * 60 + int(mm)
        except (ValueError, AttributeError):
            return -1
    reviews: list[ReviewRef] = field(default_factory=list)
    # {(publication, level): rate} — see selectivity(). Empty means alphabetical.
    selectivity: dict = field(default_factory=dict, repr=False)

    @property
    def counts(self) -> dict[int, int]:
        out = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0}
        for r in self.reviews:
            out[r.stars] += 1
        return out

    def at(self, stars: int) -> list[ReviewRef]:
        """
        Reviews at a given star level, scarcest verdict first.

        Four stars from Chortle, which gives them to a quarter of what it sees,
        is a different statement from four stars from a publication that gives
        them to four fifths. Listing the hardest-won first tells the reader
        something true without ranking one publication above another as a matter
        of editorial opinion — the order comes from their own record.

        Falls back to alphabetical when no rates are available, so this stays
        deterministic rather than arbitrary.
        """
        here = [r for r in self.reviews if r.stars == stars]
        return sorted(
            here,
            key=lambda r: (self.selectivity.get((r.publication, stars), 1.0),
                           r.publication.casefold()),
        )

    @property
    def total(self) -> int:
        return len(self.reviews)

    @property
    def mean(self) -> float:
        """Mean star rating as a number, for ranking. See `average` for display."""
        return sum(r.stars for r in self.reviews) / len(self.reviews) if self.reviews else 0.0

    @property
    def average(self) -> str:
        """
        Mean star rating, one decimal place.

        This is NOT what the leaderboard ranks on — see rank_key. It exists because search engines expect an aggregate rating,
        and it is displayed on the show page so the structured data matches what
        a reader actually sees.
        """
        if not self.reviews:
            return ""
        return f"{sum(r.stars for r in self.reviews) / len(self.reviews):.1f}"

    @property
    def page(self) -> str:
        """Path of this show's own page, relative to the site root."""
        return f"show/{self.id}/"

    @property
    def ranked(self) -> bool:
        """Shows with no 4- or 5-star reviews are listed separately, not ranked."""
        c = self.counts
        return (c[5] + c[4]) > 0


def rank_key(show: Show) -> tuple:
    """
    Sort key. Python compares tuples left to right, and negating a number turns
    an ascending sort into a descending one.

    Olympic, with medals that can be taken away. A 5-star review is a gold and a
    4-star a silver, exactly as before — any gold still outranks any number of
    silvers. What is new is that poor reviews cancel them: a 1-star cancels a
    gold, a 2-star cancels a silver, and 3-stars break ties. So a show with two
    5-star reviews and a 1-star now ranks below a show with a single clean
    5-star, which is the honest reading of its record.

    This replaces total review count as the tiebreak, which had it backwards:
    among shows with identical 5- and 4-star records, the one with an EXTRA
    3-star review ranked higher, because more reviews counted as better. That
    put a show averaging 4.0 above one averaging 4.5.

    Deliberately NOT a points sum. Scoring 5s and 4s as +3/+2 and summing lets
    three silvers outrank a gold: on the 2025 data it put a show with no 5-star
    reviews at all into the top 12, and pushed a perfect three-5-star record
    into second place. Lexicographic ordering is what makes this Olympic.

    The displayed figure stays the plain average, which is a fact about the
    show. This key decides order only — it is not a score anyone is shown.
    """
    c = show.counts
    return (
        -(c[5] - c[1]),             # golds, less the 1-star reviews that cancel them
        -(c[4] - c[2]),             # then silvers, less the 2-stars
        c[3],                       # then fewest 3-star reviews
        -show.mean,                 # then the better average
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
            "SELECT id, title, performer, presented_by, edfringe_url, festival, "
            "genre, subgenre, venue, start_time FROM shows")
    else:
        rows = conn.execute(
            """SELECT id, title, performer, presented_by, edfringe_url, festival,
                       genre, subgenre, venue, start_time
                 FROM shows WHERE year = ?""", (year,))
    from .programme import label
    for row in rows:
        shows[row["id"]] = Show(
            row["id"], row["title"], row["performer"],
            presented_by=row["presented_by"] or "",
            url=row["edfringe_url"] or None,
            genre=label(row["festival"] or "", row["genre"] or "", row["subgenre"] or "")
            if row["edfringe_url"] else "",
            # Only a show linked to its programme entry gets a badge, and the
            # same applies to the filter: an unlinked show has no classification
            # we can stand behind, so it answers to no genre rather than a guess.
            festival=(row["festival"] or "") if row["edfringe_url"] else "",
            section=(row["genre"] or "") if row["edfringe_url"] else "",
            subgenre=(row["subgenre"] or "") if row["edfringe_url"] else "",
            venue=row["venue"] or "",
            start_time=row["start_time"] or "",
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
    rates = selectivity(conn, year)
    for show in shows:
        show.selectivity = rates
    ranked = sorted([s for s in shows if s.ranked], key=rank_key)
    rest = sorted([s for s in shows if not s.ranked], key=lambda s: s.title.casefold())
    return ranked, rest


def positions(ranked: list[Show]) -> list[tuple[int, Show]]:
    """
    NO LONGER USED for display — see placement(), which resolves ties by
    scarcity instead of sharing a number. Kept because it is the honest answer
    to "who is level with whom", which is what the tie test still needs.

    Attach positions, sharing a number between genuinely tied shows
    (two shows on 3x5 and 1x4 are both 2nd, and the next show is 4th).

    Tied means the whole ranking key matches, not just the medal counts. Testing
    only the 5- and 4-star counts made every tiebreak invisible: two shows could
    be deliberately ordered — one of them carrying two 3-star reviews the other
    did not — and still be shown as equal. A reader comparing them would see one
    above the other with the same number beside it and no way to tell why.

    The title is excluded because it is only there to keep the sort stable;
    being alphabetically earlier is not a way of being better.
    """
    out: list[tuple[int, Show]] = []
    last_key, last_pos = None, 0
    for index, show in enumerate(ranked, start=1):
        key = rank_key(show)[:-1]       # everything except the alphabetical tiebreak
        if key != last_key:
            last_pos, last_key = index, key
        out.append((last_pos, show))
    return out


def scarcity(conn: sqlite3.Connection, year: int | None = None) -> dict[str, float]:
    """
    {show_id: how hard-won its top reviews were}.

    A five from Chortle, who give one to 8% of what they review, is a rarer thing
    than a five from a publication that gives them to a third of the bill. This
    adds up 1/rate for each four- and five-star review a show holds, using the
    publication's rate of going AT LEAST that high — which also means a five
    counts for more than a four without having to say so, since the rate of
    giving five is always the lower of the two.

    Used only to separate shows the main ranking has tied. It is not a second
    opinion about which show is better; it is a way of ordering equals that is
    about the reviews rather than the alphabet.
    """
    rates = selectivity(conn, year)
    scores: dict[str, float] = {}
    rows = conn.execute(
        """SELECT r.show_id, r.publication, r.stars FROM reviews r
             JOIN shows s ON s.id = r.show_id
            WHERE r.stars >= 4 AND (? IS NULL OR s.year = ?)""",
        (year, year),
    )
    for show_id, publication, stars in rows:
        rate = rates.get((publication, stars))
        if not rate:
            continue
        scores[show_id] = scores.get(show_id, 0.0) + 1.0 / rate
    return scores


def placement(conn: sqlite3.Connection, ranked: list[Show],
              year: int | None = None) -> list[tuple[int, Show]]:
    """The order everything uses: the page, the card, and the climb detection."""
    return strict_positions(ranked, scarcity(conn, year))


def strict_positions(ranked: list[Show], scores: dict[str, float]) -> list[tuple[int, Show]]:
    """
    Positions 1..N with nothing shared, ties broken by scarcity.

    The site shows tied shows sharing a number, which is honest: they are level.
    A published list of twenty cannot do that — it would run 1, 2, 8, 8, 10 and
    read as a mistake — so the ties are resolved rather than displayed, by which
    show's top reviews were the harder to come by.
    """
    # rank_key ends with the title, to keep the sort stable. Sorting on the whole
    # key makes every key unique, so the scarcity term is never reached and the
    # order comes out alphabetical — which is exactly what this is meant to
    # replace. Compare everything except that last element, as positions() does
    # when deciding what counts as tied.
    order = sorted(ranked, key=lambda s: (rank_key(s)[:-1],
                                          -scores.get(s.id, 0.0),
                                          rank_key(s)[-1]))
    return list(enumerate(order, start=1))
