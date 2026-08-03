"""
Finding a star rating on a page, and converting other scales onto the 5-star scale.

Publications express ratings in wildly different ways. This module tries a series
of strategies from cheapest/most-reliable to most-fragile, and stops at the first
one that works.

Every result carries the ORIGINAL rating as the publication gave it, so the website
can always show "8/10" next to the four stars we converted it into. We never throw
away what they actually said.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass

# Characters different sites use for a filled or an empty star.
# Keep these sets disjoint: a character in both would be counted twice.
FILLED_STARS = "★⭐✭✦"   # ★ ⭐ ✭ ✦
EMPTY_STARS = "☆✩✬"          # ☆ ✩ ✬

WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
}


@dataclass
class Rating:
    """A rating we found, normalised to whole stars but remembering the original."""

    stars: int              # 1-5, what the leaderboard counts
    original: str           # exactly what the publication showed, e.g. "8/10"
    converted: bool         # True if we rescaled from a non-5-point scale
    rounded: bool           # True if the true value fell between two whole stars
    method: str             # which strategy found it, for debugging

    @property
    def note(self) -> str:
        """Short human explanation shown on the website next to converted ratings."""
        if self.rounded:
            return f"{self.original} → {self.stars}★ (rounded down)"
        if self.converted:
            return f"{self.original} → {self.stars}★"
        return ""


def to_five_star(value: float, maximum: float) -> tuple[int, bool]:
    """
    Rescale `value` out of `maximum` onto a 1-5 whole-star scale.

    Returns (stars, was_rounded).

    Ties round DOWN, deliberately. A publication that gives 9/10 has said
    "very good but not perfect"; promoting that to five stars would put a show
    on the top line of the leaderboard on the strength of a rating nobody gave
    it. Rounding down can only ever understate a show, which is the safe
    direction of error when 5-star counts decide the ranking.
    """
    if maximum <= 0:
        raise ValueError("maximum must be positive")

    scaled = value * 5.0 / maximum
    stars = math.ceil(scaled - 0.5)          # round-half-down
    stars = max(1, min(5, stars))
    was_rounded = abs(scaled - stars) > 1e-9
    return stars, was_rounded


# Anything below this many stars is treated as a failed parse rather than a real
# rating. Nobody publishes a quarter-star review, so a value that low means the
# selector or regex grabbed the wrong number — clamping it up to one star would
# bury that mistake in the data instead of discarding it.
MIN_PLAUSIBLE_STARS = 0.75


def _make(value: float, maximum: float, original: str, method: str) -> Rating | None:
    if not (0 < value <= maximum):
        return None
    if value * 5.0 / maximum < MIN_PLAUSIBLE_STARS:
        return None
    stars, rounded = to_five_star(value, maximum)
    return Rating(
        stars=stars,
        original=original,
        converted=(maximum != 5),
        rounded=rounded,
        method=method,
    )


# --------------------------------------------------------------------------
# Strategy 1: schema.org structured data. Unambiguous when present.
# --------------------------------------------------------------------------

def from_jsonld(html: str) -> Rating | None:
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.S | re.I,
    ):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for node in _walk(data):
            if not isinstance(node, dict):
                continue
            rv = node.get("ratingValue")
            if rv is None:
                continue
            try:
                value = float(rv)
                maximum = float(node.get("bestRating", 5))
            except (TypeError, ValueError):
                continue
            original = f"{_tidy(value)}/{_tidy(maximum)}"
            found = _make(value, maximum, original, "jsonld")
            if found:
                return found
    return None


def _walk(node):
    """Yield every dict/list nested anywhere inside a JSON structure."""
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


# --------------------------------------------------------------------------
# Strategy 2: literal star characters, e.g. "★★★★☆" or "4⭐⭐⭐⭐"
# --------------------------------------------------------------------------

def from_star_chars(text: str) -> Rating | None:
    """
    Count runs of star characters. We take the LONGEST run in the text, because
    pages often contain a stray star in a sidebar or a "related reviews" list.
    """
    pattern = f"[{FILLED_STARS}{EMPTY_STARS}]{{3,5}}"
    best = None
    for run in re.findall(pattern, text):
        filled = sum(1 for ch in run if ch in FILLED_STARS)
        total = len(run)
        # A run of 5 mixed filled/empty is a classic "★★★★☆" rating.
        # A run of only filled stars is a rating equal to its own length.
        if total == 5:
            value = filled
        elif all(ch in FILLED_STARS for ch in run):
            value = total
        else:
            continue
        if best is None or value > best[0]:
            best = (value, run)

    if best is None:
        return None
    value, run = best
    return _make(value, 5, run, "star_chars")


# --------------------------------------------------------------------------
# Strategy 2b: half stars, e.g. "3.5***" or "3 1/2 ***" or "4½"
#
# This MUST run before the plain asterisk and numeric strategies. One4Review
# writes three-and-a-half stars as "3.5***", and a naive asterisk rule reads the
# digit next to the asterisks as a 5. That silently promotes a middling review
# to the top tier of the leaderboard, which is the single worst error this
# program can make.
# --------------------------------------------------------------------------

# NFKC normalisation rewrites "½" as "1⁄2" using U+2044 FRACTION SLASH, not an
# ASCII "/", so both slashes have to be accepted here.
#
# The trailing lookaheads are load-bearing. Without them "21/22" in a listings
# date range ("21/22, 25 - 27, 29/30 Aug") parses as 2 followed by 1/2, and a
# venue listing page is scored two and a half stars. Requiring that nothing
# numeric follows keeps real half-star notation and rejects date ranges.
HALF_PATTERNS = (
    r"\b([1-4])\s*[.,]\s*5\b(?![\d/⁄.,])",           # 3.5
    r"\b([1-4])\s*(?:1\s*[/⁄]\s*2|½)(?![\d/⁄])",  # 3 1/2  or  3½
)


def from_half_stars(text: str) -> Rating | None:
    for pattern in HALF_PATTERNS:
        m = re.search(pattern, text)
        if not m:
            continue
        value = float(m.group(1)) + 0.5
        return _make(value, 5, f"{_tidy(value)}/5", "half_stars")
    return None


# --------------------------------------------------------------------------
# Strategy 3: asterisks used as stars, e.g. "3***" or "***** 5 stars"
# --------------------------------------------------------------------------

def from_asterisks(text: str) -> Rating | None:
    """
    One4Review writes ratings as "3***", "***** 5 stars", "4****". When a digit
    sits next to the asterisks we trust the digit, since it is unambiguous.

    A bare run of asterisks is only trusted at length 3 or more. Shorter runs are
    too easily emphasis markup or a footnote marker, and a missed rating is a far
    cheaper mistake than a wrong one.
    """
    m = re.search(r"(?<![\w*])([1-5])\s*(\*{1,5})(?![\w*])", text)
    if m:
        value = int(m.group(1))
        return _make(value, 5, f"{value}{m.group(2)}", "asterisks")

    runs = re.findall(r"(?<![\w*])(\*{3,5})(?![\w*])", text)
    if not runs:
        return None
    value = max(len(r) for r in runs)
    return _make(value, 5, "*" * value, "asterisks")


# --------------------------------------------------------------------------
# Strategy 4: numeric ratings in prose, e.g. "4/5", "8/10", "80%", "four stars"
# --------------------------------------------------------------------------

def from_numeric(text: str) -> Rating | None:
    # "4/5", "4 out of 5", "8 / 10"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:/|out of)\s*(\d+)\b", text, re.I)
    if m:
        value, maximum = float(m.group(1)), float(m.group(2))
        if maximum in (4, 5, 6, 10, 100):
            return _make(value, maximum, f"{_tidy(value)}/{_tidy(maximum)}", "numeric")

    # NOTE: bare percentages are deliberately NOT treated as ratings here.
    # Free text and page markup are full of them — layout widths, "up 4% on last
    # year" in a news piece — and trusting them rated a Fringe programme-launch
    # announcement at one star. A percentage only counts as a rating when a
    # publication's CSS rule in sources.yaml points at the element holding it,
    # which from_css_rule handles with an explicit `scale: 100`.

    # "four stars", "4 stars"
    m = re.search(r"\b(one|two|three|four|five|[1-5])[\s-]+stars?\b", text, re.I)
    if m:
        value = WORD_NUMBERS[m.group(1).lower()]
        return _make(value, 5, f"{value} stars", "words")

    return None


# --------------------------------------------------------------------------
# Strategy 4b: word-grade badges (FringeReview)
#
# FringeReview dropped star ratings deliberately and awards named badges instead,
# encoded in an image filename: badges/new/HIGHLY_RECOMMENDED_SHOW.png
#
# Two things to keep in mind when reading the mapping below:
#
#   * FringeReview states there is "deliberate overlap" between its ratings and
#     rejects a strict hierarchy. Putting numbers on them is OUR editorial
#     decision, not theirs, which is why the badge name is always displayed
#     alongside the stars on the site.
#   * They only publish "Good" or better; weaker shows get private feedback. So
#     this publication can never contribute a 1- or 2-star rating. It can lift a
#     show up the leaderboard but never pull one down.
# --------------------------------------------------------------------------

FRINGEREVIEW_BADGES = {
    "OUTSTANDING_SHOW":                 (5, "Outstanding Show"),
    "MUST_SEE_SHOW":                    (5, "Must See Show"),
    "EXCELLENT_SHOW":                   (5, "Excellent Show"),
    "HIGHLY_RECOMMENDED_SHOW":          (4, "Highly Recommended"),
    "VERY_GOOD_SHOW":                   (4, "Very Good Show"),
    "RECOMMENDED_SHOW":                 (3, "Recommended"),
    "GOOD_SHOW":                        (3, "Good Show"),
    # Youth theatre strand uses its own badge art for the same grades.
    "OUTSTANDING_YOUTH_THEATRE":        (5, "Outstanding Youth Theatre"),
    "HIGHLY_RECOMMENDED_YOUTH_THEATRE": (4, "Highly Recommended Youth Theatre"),
    "VERY_GOOD_YOUTH_THEATRE":          (4, "Very Good Youth Theatre"),
    "RECOMMENDED_YOUTH_THEATRE":        (3, "Recommended Youth Theatre"),
}

# These describe the KIND of work rather than its quality. "Daring Work" sits on
# a different axis from "Excellent Show", not above or below it, so there is no
# honest star value for them and the review is skipped.
UNRANKED_BADGES = {
    "DARING_WORK", "EXCITING_WORK", "GROUNDBREAKING_WORK", "HIDDEN_GEM",
}

BADGE_VOCABULARIES = {"fringereview": FRINGEREVIEW_BADGES}


def from_badge(html: str, rule: dict) -> Rating | None:
    vocabulary = BADGE_VOCABULARIES.get(rule.get("vocabulary", "fringereview"), {})
    for name in re.findall(r"badges/[^\"']*?/([A-Z_0-9]+)\.(?:png|jpg|svg)", html):
        if name in UNRANKED_BADGES:
            return None
        if name in vocabulary:
            stars, label = vocabulary[name]
            return Rating(
                stars=stars,
                original=label,
                converted=True,     # ensures the badge name shows on the site
                rounded=False,
                method="badge",
            )
    return None


# --------------------------------------------------------------------------
# Strategy 4c: count repeated markers
#
# Some sites draw the rating as N copies of a star image and simply omit the
# empty ones, so the rating is the number of times a marker appears. Chortle
# renders <img src="/images/layout/star.png" alt="review star"/> once per star.
# --------------------------------------------------------------------------

def from_marker_count(html: str, rule: dict) -> Rating | None:
    marker = rule.get("marker")
    if not marker:
        return None
    count = len(re.findall(marker, html))
    if not 1 <= count <= 5:
        # Zero means no rating on this page; more than five means the marker is
        # matching something else and the number cannot be trusted.
        return None
    return _make(count, 5, f"{count}/5", "marker_count")


# --------------------------------------------------------------------------
# Strategy 5: a per-publication CSS rule from sources.yaml
# --------------------------------------------------------------------------

def from_css_rule(html: str, rule: dict) -> Rating | None:
    """
    `rule` comes from sources.yaml, e.g.:

        rating:
          type: css
          selector: "span.number.rating .value"
          scale: 100          # the publication's own maximum

    Some sites encode the rating as a CSS width percentage
    (`<span style="width: 80%">`), which `width_style: true` handles.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one(rule["selector"])
    if node is None:
        return None

    maximum = float(rule.get("scale", 5))

    if rule.get("width_style"):
        style = node.get("style", "")
        m = re.search(r"width:\s*([\d.]+)%", style)
        if not m:
            return None
        value = float(m.group(1))
        return _make(value, 100, f"{_tidy(value)}%", "css_width")

    raw = node.get_text(" ", strip=True)
    m = re.search(r"\d+(?:\.\d+)?", raw)
    if not m:
        return None
    value = float(m.group())
    label = f"{_tidy(value)}/{_tidy(maximum)}" if maximum != 100 else f"{_tidy(value)}%"
    return _make(value, maximum, label, "css")


# --------------------------------------------------------------------------
# The cascade
# --------------------------------------------------------------------------

def find_rating(*, title: str = "", summary: str = "", html: str = "",
                rule: dict | None = None) -> Rating | None:
    """
    Try every strategy in order of reliability and return the first hit.

    Order matters. Structured data beats a regex; a rating in the article's own
    title beats one scraped from the page body, because page bodies contain
    sidebars full of other shows' ratings.
    """
    rule = rule or {}
    kind = rule.get("type", "auto")

    if kind == "none":
        return None

    if kind == "css":
        return from_css_rule(html, rule)

    if kind == "badge":
        return from_badge(html, rule)

    if kind == "marker_count":
        return from_marker_count(html, rule)

    if html:
        found = from_jsonld(html)
        if found:
            return found

    # Half-star notation is only ever trusted in the TITLE. One4Review writes
    # "3.5***" there, which is unambiguous — but the same shapes occur constantly
    # in article prose ("runs 2 1/2 hours", "21/22 Aug", "£3.50"), and several
    # feeds put the whole article in the summary. Scanning body text for half
    # stars scored a venue listings page 2.5 stars.
    if title:
        found = from_half_stars(unicodedata.normalize("NFKC", title))
        if found:
            return found

    # Titles and summaries are high-signal: they describe THIS review only.
    for text in (title, summary):
        if not text:
            continue
        clean = unicodedata.normalize("NFKC", text)
        for strategy in (from_star_chars, from_asterisks, from_numeric):
            found = strategy(clean)
            if found:
                return found

    if html:
        from bs4 import BeautifulSoup

        body = BeautifulSoup(html, "lxml").get_text(" ", strip=True)[:4000]
        for strategy in (from_star_chars, from_numeric):
            found = strategy(body)
            if found:
                return found

    return None


def split_roundup(html: str) -> list[tuple[str, Rating]]:
    """
    Pull the individual shows out of a multi-show round-up.

    A round-up reviews three to five shows in one article, each with its own
    star rating. Read as a single review it is worse than useless: the cascade
    finds the first rating and pins it to a headline naming five shows, which is
    how "All #EdFringe 2025 Made in Edinburgh reviews" ended up on the board.

    The shape is consistent because the rating is printed alongside the show it
    belongs to: "Melbourne Symphony Orchestra ★★★★★" in its own element. So each
    element ending in stars is one review, and the text before them is the show.

    Returns (show title, rating) pairs. Titles repeat within an article - the
    same heading often appears in a summary list and again above the copy - so
    the first occurrence of each wins.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[str, Rating]] = []
    seen: set[str] = set()

    for tag in soup.find_all(["strong", "b", "h2", "h3", "h4"]):
        text = " ".join(tag.get_text().split())
        if not text or not any(ch in text for ch in FILLED_STARS):
            continue

        # Everything before the first star is the show; everything after is the
        # star run itself. A trailing venue stays attached deliberately - it is
        # part of how the publication names the show, and the matcher already
        # copes with "Show, Venue".
        first = min(i for i, ch in enumerate(text) if ch in FILLED_STARS)
        title = text[:first].strip(" -–—:,|").strip()
        rating = from_star_chars(text[first:])
        if rating is None or not title or len(title) < 3:
            continue

        key = title.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append((title, rating))
    return out


def _tidy(n: float) -> str:
    """Render 5.0 as '5' but 4.5 as '4.5'."""
    return str(int(n)) if float(n).is_integer() else str(n)
