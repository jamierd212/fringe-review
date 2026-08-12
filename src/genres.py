"""
The genre vocabulary, and what a reader is offered to filter by.

The Fringe publishes two fields: a section (COMEDY, THEATRE) and a comma-joined
list of free tags (Stand-up, Solo show, LGBTQ+). Only the section is a real
hierarchy - the tags are a flat vocabulary reused across sections, so Comedy and
Theatre both carry Storytelling, and Circus carries Dance. Scoping each tag to
the section it appears in is what makes "Comedy - Stand-up" mean something.

Only the Fringe classifies its shows. EIF publishes no genre anywhere in its
programme: the event pages carry no category field and their own what's-on
filters are built client-side, so an EIF show can be found by name or venue but
not by genre. The free fringe publishes none either.

Those two festivals stand at the top of the list as choices in their own right.
It is the one honest thing to offer for a show whose programme classifies
nothing - "which festival is this" is a question we can answer, where "what kind
of show is this" is not - and it is what a reader looking for the International
Festival would reach for anyway.
"""

from __future__ import annotations

from collections import Counter

# The Fringe's section names as the Fringe writes them. Its API returns machine
# forms - CHILDRENS_SHOWS, OPERA - and title-casing those gives "Childrens
# Shows", and "Opera" for a section that is mostly musicals.
SECTIONS = {
    "COMEDY": "Comedy",
    "THEATRE": "Theatre",
    "OPERA": "Musicals & Opera",
    "MUSICALS_AND_OPERA": "Musicals & Opera",
    "CIRCUS": "Circus",
    "CHILDRENS_SHOWS": "Children's Shows",
    "CABARET": "Cabaret & Variety",
    "MUSIC": "Music",
    "DANCE": "Dance",
    "SPOKEN_WORD": "Spoken Word",
    "EXHIBITIONS": "Exhibitions",
    "EVENTS": "Events",
}

# Festivals that publish no classification at all, offered as choices in their
# own right. The Fringe is absent deliberately: its shows already have sections,
# so a "Fringe" entry would select nearly everything and say nothing.
FESTIVALS = {
    "eif": "EIF",
    "freefringe": "Free Fringe",
}

# Tags describing who made a show or who it is for, rather than what kind of
# show it is. They still appear on the show page, where they are the artist's
# own description; they are not offered as genres to filter by, because a
# reader choosing a genre is choosing a kind of show.
NOT_A_GENRE = {
    "lgbtq+",
    "artist(s) of colour",
    "neurodiversity-led",
    "disabled-led",
}

# Tags whose shape capitalize() would lose.
SPELLINGS = {
    "sci-fi": "Sci-fi",
    "hip hop": "Hip hop",
    "science and technology": "Science and technology",
    "true-life": "True-life",
    "site-specific": "Site-specific",
    "stand-up": "Stand-up",
    "family-friendly": "Family-friendly",
    "game show": "Game show",
    "sketch show": "Sketch show",
    "solo show": "Solo show",
    "new writing": "New writing",
    "live music": "Live music",
    "musical theatre": "Musical theatre",
    "musical comedy": "Musical comedy",
    "physical theatre": "Physical theatre",
    "character comedy": "Character comedy",
    "alternative comedy": "Alternative comedy",
    "dark comedy": "Dark comedy",
    "performance art": "Performance art",
    "spoken word": "Spoken word",
}


def section(genre: str) -> str:
    """The display name of a top-level section."""
    raw = (genre or "").strip()
    if not raw:
        return ""
    return SECTIONS.get(raw.upper(), raw.replace("_", " ").title())


def tag(name: str) -> str:
    """The display name of a sub-genre tag."""
    key = (name or "").strip().lower()
    return SPELLINGS.get(key, key.capitalize())


def tags_of(subgenre: str) -> list[str]:
    """
    The filterable tags on a show, lowercased for matching.

    The same tag arrives cased differently on different shows - "Dark comedy"
    and "Dark Comedy", "Artist(s) of colour" and "Artist(s) of Colour" - so
    everything is matched in lower case and displayed from one spelling.
    """
    out = []
    for part in (subgenre or "").split(","):
        key = part.strip().lower()
        if key and key not in NOT_A_GENRE and key not in out:
            out.append(key)
    return out


def keys_for(festival: str, genre: str, subgenre: str) -> list[str]:
    """
    Every filter value this show should answer to.

    A show tagged Stand-up under Comedy answers to both "COMEDY" and
    "COMEDY|stand-up", so choosing the section keeps it and so does choosing
    the sub-genre. A show from a festival that publishes no classification
    answers to its festival instead.
    """
    fest = (festival or "").strip().lower()
    if fest in FESTIVALS:
        # "@" cannot appear in a section or a tag, so a festival key can never
        # collide with one.
        return [f"@{fest}"]
    raw = (genre or "").strip().upper()
    if not raw:
        return []
    return [raw] + [f"{raw}|{t}" for t in tags_of(subgenre)]


def options(shows) -> tuple[list, list]:
    """
    (sections, subgenres) for the filter, each as (value, label, count).

    Ordered by how many shows carry them, so the genres a reader is most likely
    to want are not buried under one-off tags - the same reasoning as the venue
    areas. Sub-genres are grouped under their section so the list reads as
    Comedy's tags, then Theatre's, rather than one alphabetical run.
    """
    sections: Counter[str] = Counter()
    subs: Counter[tuple[str, str]] = Counter()
    fests: Counter[str] = Counter()
    for show in shows:
        fest = (getattr(show, "festival", "") or "").strip().lower()
        if fest in FESTIVALS:
            fests[fest] += 1
            continue
        raw = (getattr(show, "section", "") or "").strip().upper()
        if not raw:
            continue
        sections[raw] += 1
        for t in tags_of(getattr(show, "subgenre", "") or ""):
            subs[(raw, t)] += 1

    # Festivals first, in the order they are written above rather than by size:
    # there are only two, and EIF above Free Fringe reads better than a ranking
    # that would flip whenever one of them was reviewed more.
    ordered = [(f"@{k}", FESTIVALS[k], fests[k]) for k in FESTIVALS if fests[k]]
    ordered += [(raw, section(raw), n) for raw, n in sections.most_common()]
    sub_options = []
    for raw, label, _ in ordered:
        for (sec, t), n in sorted(subs.items(), key=lambda kv: (-kv[1], kv[0][1])):
            if sec == raw:
                sub_options.append((f"{raw}|{t}", f"{label} – {tag(t)}", n))
    return ordered, sub_options
