"""
Turning a messy review headline into a show title we can match on.

Publications write the same show a dozen ways:

    "Review: Sam Campbell: Companion Piece ★★★★☆"
    "Sam Campbell – Companion Piece, Pleasance Courtyard"
    "Companion Piece"

This module reduces all of those to a comparable form, and guesses which part
is the performer and which is the show.
"""

from __future__ import annotations

import html as html_lib
import re
import unicodedata

from .ratings import EMPTY_STARS, FILLED_STARS

# Words publications bolt onto the front of a headline, e.g.
# "Review:", "Edinburgh Fringe 2025:", "Edinburgh International Film Festival 2025:".
#
# The optional year matters more than it looks. Without it, every headline from a
# publication that prefixes "Edinburgh Fringe 2025:" keeps that boilerplate in its
# title — and since fuzzy matching compares whole strings, two unrelated shows then
# score ~75% against each other purely on the shared prefix. That produces garbage
# candidate shortlists and, worse, risks merging different shows.
PREFIXES = re.compile(
    r"^\s*(?:"
    r"review|preview"
    r"|ed\s*fringe|edfringe|eif"
    r"|edinburgh(?:\s+international)?(?:\s+(?:film\s+festival|festival|fringe))*"
    r"|fringe|theatre|comedy"
    r")\s*(?:\d{4})?\s*[:\-–—]\s*",
    re.I,
)

# Trailing venue/location, e.g. "– Pleasance Courtyard, Edinburgh"
VENUE_TAIL = re.compile(
    r"\s*[–—\-|,]\s*[^,–—|]{0,60}?,?\s*Edinburgh\s*$", re.I
)

# UK-wide publications title reviews "Show - Venue", so the tail is a place, not
# part of the name. Without this "Ordinary Elephant - Edinburgh Traverse Bar"
# split into performer "Ordinary Elephant" and show "Edinburgh Traverse Bar",
# and the venue went on the leaderboard as the show.
#
# Only distinctive venue words are listed. Generic ones (hall, bar, club,
# centre) would strip real titles: "Raise the Bar" is a show, not a venue.
VENUE_WORDS = (
    r"(?:theatre|theater|playhouse|summerhall|traverse|assembly|pleasance|"
    r"underbelly|gilded\s+balloon|zoo\s+southside|roxy|bristo|cowgate|"
    r"courtyard|boulevard|printmakers|monkey\s+barrel|dome|arena)"
)
VENUE_TAIL_NAMED = re.compile(
    # The tail may itself contain commas — "Pleasance Courtyard, Bunker 1" is
    # one venue, not a venue and something else — so only the SEPARATOR is
    # restricted, not the text after the venue word.
    rf"\s*[–—|@]\s*[^–—|@]{{0,40}}\b{VENUE_WORDS}\b.{{0,40}}$"
    rf"|\s*[-,]\s*[^–—|@]{{0,40}}\b{VENUE_WORDS}\b.{{0,40}}$", re.I
)

# Publications append the festival to the headline ("Little Pink Dress –
# Edinburgh Festival Fringe"). Left in place, split_performer reads the dash as
# performer/show and files the review under a show called "Edinburgh Festival
# Fringe" — which is exactly what reached first place on the 2026 board.
FESTIVAL_TAIL = re.compile(
    r"\s*[–—\-|,]\s*(?:the\s+)?"
    r"(?:edinburgh\s+)?(?:international\s+)?(?:festival\s+)?"
    r"(?:fringe|festivals?|edfringe|eif)"
    r"(?:\s+\d{4})?\s*$", re.I
)

# Rating fragments embedded in titles, e.g. "4⭐⭐⭐⭐", "3.5***", "5 stars", "(4/5)".
#
# Asterisk runs are only stripped when a digit is attached or they sit at the end
# of the headline. A bare run mid-sentence is usually a censored word — stripping
# it would turn "You Are All C**ts" into "You Are All C ts".
RATING_FRAGMENT = re.compile(
    # (?<!\d) so the digit swallowed before a star run cannot be the last digit
    # of a year: "EdFringe 2025 ★★★★" was being reduced to "EdFringe 202".
    rf"[\(\[]?\s*(?:(?<!\d)\d\s*)?[{FILLED_STARS}{EMPTY_STARS}]+\s*[\)\]]?"
    rf"|[\(\[]?\s*\d\s*(?:[.,]\s*5|1\s*[/⁄]\s*2|½)?\s*\*{{1,5}}\s*[\)\]]?"
    rf"|\s\d\s*(?:[.,]5|1\s*[/⁄]\s*2|½)\s*$"
    rf"|\*{{2,5}}\s*$"
    rf"|[\(\[]?\s*\d(?:[.,]\d)?\s*(?:/|out of)\s*\d+\s*[\)\]]?"
    rf"|\b(?:one|two|three|four|five|[1-5])[\s-]+stars?\b",
    re.I,
)

# Newspapers title reviews "Show Name review – witty summary of the verdict".
# Everything from " review" onwards is the paper's editorialising, not the show.
REVIEW_TAIL = re.compile(r"\s+review\b.*$", re.I)

# Words that describe a SECTION rather than name a show. A headline made only of
# these before the word "review" is a label, not a title.
SECTION_WORDS = {
    "edinburgh", "international", "festival", "festivals", "fringe", "eif",
    "music", "theatre", "dance", "art", "comedy", "books", "book", "opera",
    "classical", "musicals", "musical", "circus", "cabaret", "kids",
    "children", "spoken", "word", "review", "reviews", "and", "the", "at",
}

SECTION_REVIEW = re.compile(r"^(?P<head>[^:]{0,70}?)\s+reviews?\s*:\s*(?P<tail>.+)$", re.I)


def _prefer_after_review(text: str) -> str:
    """
    "Edinburgh International Festival music review: Closing Concert" names the
    show AFTER the colon, not before it.

    Stripping from the word "review" onwards — right for "Chappell Roan,
    Edinburgh review: 'joyous'", where a pull-quote follows — threw the show
    name away here and left the section label, which then ranked 23rd on the
    board with five stars.

    So the head is only discarded when it says nothing: every word in it is a
    festival or section word. A real title contains something else.
    """
    m = SECTION_REVIEW.match(text)
    if not m:
        return text
    head_words = re.findall(r"[A-Za-z]+", m.group("head").lower())
    if head_words and all(w in SECTION_WORDS for w in head_words):
        return m.group("tail").strip()
    return text

SEPARATORS = re.compile(r"\s*[:–—]\s*|\s+-\s+")


def clean_title(raw: str) -> str:
    """Strip ratings, prefixes and venue tails, leaving the human-readable title."""
    # Index and listing discovery read titles straight out of HTML with a regex,
    # so entities survive where a feed parser would have decoded them. Left
    # alone they reach the page as "Rat&#39;s Ass" and stop the show matching
    # its programme entry.
    text = html_lib.unescape(raw or "")
    text = unicodedata.normalize("NFKC", text)
    text = _unify_punctuation(text)
    text = RATING_FRAGMENT.sub(" ", text)
    # Headlines can stack prefixes ("Review: Edinburgh Fringe 2025: Diva"), so
    # strip repeatedly until nothing more comes off.
    for _ in range(3):
        stripped = PREFIXES.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = _prefer_after_review(text)
    text = REVIEW_TAIL.sub("", text)
    text = VENUE_TAIL.sub("", text)
    text = VENUE_TAIL_NAMED.sub("", text)
    text = FESTIVAL_TAIL.sub("", text)
    text = re.sub(r"\s*\(\s*(WIP|work in progress|preview)\s*\)\s*", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" -–—:,|")


def normalise(raw: str) -> str:
    """
    The matching key. Aggressively flattened: this is never shown to a user,
    it exists only so two spellings of the same show collide.
    """
    text = clean_title(raw).casefold()
    text = text.replace("&", " and ")
    text = _strip_accents(text)
    text = re.sub(r"\b(the|a|an)\b", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def split_performer(raw: str) -> tuple[str | None, str]:
    """
    Guess "Performer: Show Title" splits.

    Returns (performer, title). Performer is None when the headline does not
    look like a performer-prefixed title.

    Heuristic: split on the first colon or dash. Treat the left side as a
    performer only if it is short and looks like a name (1-4 words, no verbs).
    Show titles genuinely containing a colon ("Hamlet: The Musical") would be
    mis-split, so we keep BOTH forms as aliases rather than committing.
    """
    title = clean_title(raw)
    parts = SEPARATORS.split(title, maxsplit=1)
    if len(parts) != 2:
        return None, title

    left, right = parts[0].strip(), parts[1].strip()
    if not left or not right:
        return None, title

    words = left.split()
    looks_like_name = (
        1 <= len(words) <= 4
        and all(w[:1].isupper() or not w[:1].isalpha() for w in words)
        and len(left) <= 40
    )
    return (left, right) if looks_like_name else (None, title)


def alias_forms(raw: str) -> list[str]:
    """
    Every normalised string that should point at this show.

    Generating a few aliases per review is what lets "Companion Piece" from one
    publication find "Sam Campbell: Companion Piece" from another.
    """
    title = clean_title(raw)
    performer, show = split_performer(raw)

    candidates = [title, show]
    if performer:
        candidates.append(f"{performer} {show}")

    seen, out = set(), []
    for c in candidates:
        key = normalise(c)
        if key and len(key) > 2 and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _unify_punctuation(text: str) -> str:
    for a, b in (
        ("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"'),
        ("–", "-"), ("—", "-"), ("…", "..."), (" ", " "),
    ):
        text = text.replace(a, b)
    return text


def _strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )
