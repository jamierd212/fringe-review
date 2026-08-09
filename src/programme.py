"""
Linking shows to their official programme entry, and reading the genre from it.

Each festival publishes a sitemap listing every show as a title-derived slug, so
matching our titles against it gives the canonical URL to link to without
fetching thousands of pages. Fetching a matched page then yields whatever
classification that festival exposes.

How much comes back varies by festival, and the difference is worth knowing:

    Fringe   4,200 shows, with `genre` and `subGenre` in the page JSON.
             This is what makes "Comedy · Stand-up" possible — the Fringe's own
             classification, not our inference.

    EIF      ~90 events plus ~1,400 archived ones. No structured genre anywhere:
             the words appear only in prose descriptions, and inferring
             "Opera" from a paragraph is guesswork we would then present as
             fact. So EIF shows are labelled by festival alone.

Only the current festival is enriched. Past programme pages are taken down once
the shows stop selling.
"""

from __future__ import annotations

import re
import sqlite3
import ssl
import time
import urllib.request
from urllib.error import HTTPError, URLError

import certifi

from .normalise import normalise

def _user_agent() -> dict:
    """
    The configured User-Agent, so there is exactly one of them.

    This module used to hold its own copy of the string. Two copies drift, and a
    crawler that identifies itself differently depending on which part of the
    code is running is worse than one that does not identify itself at all.
    """
    from .collect import load_config
    return {"User-Agent": load_config().get("defaults", {}).get(
        "user_agent", "FringeLeaderboardBot/0.1")}


UA = _user_agent()
DELAY = 1.0          # edfringe.com's robots.txt asks for Crawl-delay: 1

# macOS Python ships without a usable system CA bundle, so verification fails
# with a bare urlopen. requests (used elsewhere) bundles certifi; here we pass
# it explicitly rather than the far worse option of disabling verification.
_SSL = ssl.create_default_context(cafile=certifi.where())


_GATE = None


def _allowed(url: str) -> bool:
    """
    Consult the same robots.txt gate the collector uses.

    This module fetches festival sitemaps and show pages through its own
    urlopen, so without this it was the one part of the crawler exempt from the
    rules the site publicly promises to follow. All four festival sites permit
    us today; the point is that they would still be asked tomorrow.
    """
    global _GATE
    if _GATE is None:
        from .collect import Collector, load_config
        _GATE = Collector(load_config())
    return _GATE.allowed(url)


def _get(url: str, timeout: int = 25) -> str | None:
    if not _allowed(url):
        print(f"      ! robots.txt disallows  {url[:70]}")
        return None
    try:
        req = urllib.request.Request(url, headers=UA)
        return urllib.request.urlopen(req, timeout=timeout,
                                      context=_SSL).read().decode("utf-8", "replace")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        print(f"      ! {type(exc).__name__} {url[:70]}")
        return None


def _no_details(html: str) -> dict:
    """
    For festivals that publish no machine-readable classification.

    Inferring "Comedy" from a prose description is guesswork, and guesswork
    presented as the festival's own label is worse than no label at all.
    """
    return {}


def _json_array(html: str, key: str):
    """
    The array under `key` in the page JSON, matched by balancing brackets.

    A non-greedy regex stops at the first "]", which inside performances is the
    end of a nested list rather than the end of the array — so it returned a
    fragment that would not parse.
    """
    import json

    start = html.find(f'"{key}"')
    if start == -1:
        return None
    open_at = html.find("[", start)
    if open_at == -1:
        return None
    depth, i = 0, open_at
    while i < len(html):
        if html[i] == "[":
            depth += 1
        elif html[i] == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    try:
        return json.loads(html[open_at:i + 1])
    except ValueError:
        return None


def _start_time(html: str) -> str:
    """
    The show's usual start time as HH:MM.

    A run lists every performance separately, but they share one time — 25
    performances of Simon Munnery, all at 11:50. The most common wins, so an
    added late show or a preview at an odd hour does not become the answer.
    """
    from collections import Counter

    performances = _json_array(html, "performances") or []
    times = Counter(
        p["dateTime"][11:16]
        for p in performances
        if isinstance(p, dict) and not p.get("cancelled")
        and isinstance(p.get("dateTime"), str) and len(p["dateTime"]) >= 16
    )
    return times.most_common(1)[0][0] if times else ""


def _fringe_details(html: str) -> dict:
    """The Fringe states genre and subGenre outright in the page JSON."""
    genre = re.search(r'"genre"\s*:\s*"([^"]*)"', html)
    sub = re.search(r'"subGenre"\s*:\s*"([^"]*)"', html)
    duration = re.search(r'"duration"\s*:\s*"?(\d+)', html)
    if not genre:
        return {}
    # "venues" is an array; the first entry's title is the venue name.
    venue = re.search(r'"venues"\s*:\s*\[\s*\{\s*"title"\s*:\s*"([^"]{2,80})"', html)
    return {"genre": genre.group(1).strip(),
            "subgenre": (sub.group(1).strip() if sub else ""),
            "venue": (venue.group(1).strip() if venue else ""),
            "start_time": _start_time(html),
            "duration": (duration.group(1) if duration else "")}


def _eif_details(html: str) -> dict:
    """
    EIF publishes neither a machine-readable genre nor a venue. Both appear only
    inside prose descriptions, where "a night of dance and song" would be read as
    Dance for a concert, and a venue named in passing may not be the one playing.
    Rather than invent either and print it as fact, we record that the show is
    EIF and leave both empty.
    """
    return {"genre": "", "subgenre": "", "venue": ""}


FESTIVALS = [
    {
        "key": "fringe",
        "label": "Fringe",
        "sitemap": "https://www.edfringe.com/tickets/sitemap.xml",
        "paths": ("/whats-on/",),
        "details": _fringe_details,
    },
    {
        "key": "eif",
        "label": "EIF",
        "sitemap": "https://www.eif.co.uk/sitemap.xml",
        # /archive/ holds past years, which is how an EIF show reviewed before
        # this year's programme went live can still resolve.
        "paths": ("/events/", "/archive/"),
        "details": _eif_details,
    },
    {
        # The other half of the Fringe. Only 31% of PBH's shows appear in the
        # official edfringe programme (measured: 209 of 673, fuzzy included), so
        # without this index the free fringe is largely invisible to the gate —
        # and those are the acts least likely to have press elsewhere.
        "key": "freefringe",
        "label": "Free Fringe",
        "sitemap": "https://freefringe.org.uk/sitemap_index.xml",
        "paths": ("/shows/",),
        "details": _no_details,
    },
    {
        "key": "art",
        "label": "Art Festival",
        "sitemap": "https://edinburghartfestival.com/sitemap_index.xml",
        "paths": ("/event/",),
        "details": _no_details,
    },
]


def _sitemap_urls(sitemap: str, paths: tuple[str, ...]) -> list[str]:
    """
    Every page URL under a sitemap, following one level of sitemap index.

    The Fringe and EIF publish a flat sitemap; the Free Fringe and Art Festival
    publish an index pointing at sub-sitemaps. Reading only the top level of
    those returns sitemap URLs and no shows at all.
    """
    raw = _get(sitemap, timeout=40)
    if raw is None:
        return []
    locs = re.findall(r"<loc>(.*?)</loc>", raw)
    if not any(u.endswith(".xml") for u in locs):
        return locs

    urls: list[str] = []
    for sub in locs:
        if not sub.endswith(".xml"):
            urls.append(sub)
            continue
        time.sleep(DELAY)
        child = _get(sub, timeout=40)
        if child:
            urls += re.findall(r"<loc>(.*?)</loc>", child)
    return urls


def programme_index(festival: dict) -> dict[str, str]:
    """{normalised title: URL} for one festival's programme."""
    index: dict[str, str] = {}
    for url in _sitemap_urls(festival["sitemap"], festival["paths"]):
        if not any(p in url for p in festival["paths"]):
            continue
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        key = normalise(slug.replace("-", " "))
        # The section page itself ("/event/") slugs to the section name, which
        # would then match any review headline containing that word.
        if not key or any(p.strip("/") == slug for p in festival["paths"]):
            continue
        index.setdefault(key, url)

        # EIF disambiguates some slugs with a trailing year ("inala-2026"), which
        # no review headline will carry. Index the bare form too so the show is
        # still findable, without letting it overwrite an exact match.
        stripped = re.sub(r"\s+(19|20)\d\d$", "", key)
        if stripped != key:
            index.setdefault(stripped, url)
    return index


_KNOWN: dict[str, tuple[str, str]] | None = None


def known_shows(refresh: bool = False) -> dict[str, tuple[str, str]]:
    """
    {normalised title: (festival key, URL)} across every festival we index.

    This is the admission list: a real show appears in its own festival's
    programme, an article *about* the festival never does. Built once per run
    and cached, because it costs a handful of requests.

    Fringe first so its entry wins a collision — it carries genre data the
    others do not, and a show listed in both is a Fringe show either way.
    """
    global _KNOWN
    if _KNOWN is not None and not refresh:
        return _KNOWN
    combined: dict[str, tuple[str, str]] = {}
    for festival in FESTIVALS:
        index = programme_index(festival)
        print(f"      {festival['label']}: {len(index)} listed")
        for key, url in index.items():
            combined.setdefault(key, (festival["key"], url))
    _KNOWN = combined
    return combined


def label(festival: str, genre: str, subgenre: str) -> str:
    """
    The badge text.

    Where a festival gives us a real classification we show it, because that is
    the informative part. Where it does not, the festival name still tells a
    reader something useful — that this is the International Festival rather
    than the Fringe.
    """
    # The Fringe's own values are machine-shaped: "Childrens_Shows", "Theatre".
    genre = (genre or "").replace("_", " ").title().replace("And", "&")
    # The Fringe comma-joins every subgenre it holds, so a badge could read
    # "Childrens Shows · Musical comedy, Family-friendly" — 48 characters, wider
    # than a phone, and the page scrolled sideways because of it. The first is
    # the one that says most; the rest are noise on a row that already has a
    # title, a performer and a row of stars.
    subgenre = (subgenre or "").split(",")[0].strip()
    parts = [p for p in (genre, subgenre) if p]
    if not parts:
        # With no classification, the festival's name is worth showing only when
        # it is NOT the Fringe: "EIF" tells a reader this is the International
        # Festival, whereas "FRINGE" on a site about the Edinburgh festivals
        # says nothing at all and is just one more thing on the row.
        if not festival or festival == "fringe":
            return ""
        names = {f["key"]: f["label"] for f in FESTIVALS}
        return names.get(festival, festival.upper()).upper()
    return " · ".join(parts)


def enrich(conn: sqlite3.Connection, year: int, limit: int | None = None) -> dict[str, int]:
    """
    Attach programme URL, festival and genre to this year's unlinked shows.

    Also picks up shows the admission gate linked but could not classify. The
    gate matches against the sitemap alone, which is cheap and gives a URL and a
    festival but no genre — that only exists on the show page. Selecting purely
    on a missing URL would skip exactly those shows and quietly cost every
    tier-1 admission its genre badge. Only the Fringe publishes a genre, so no
    other festival is re-fetched looking for one that is never there.
    """
    pending = conn.execute(
        """SELECT id, title, performer, edfringe_url, festival FROM shows
            WHERE year = ?
              AND (edfringe_url IS NULL OR edfringe_url = ''
                   OR (festival = 'fringe'
                       AND (genre IS NULL OR genre = '' OR start_time IS NULL)))
            ORDER BY title""",
        (year,),
    ).fetchall()
    if not pending:
        return {"matched": 0, "missed": 0}

    indexes = []
    for fest in FESTIVALS:
        idx = programme_index(fest)
        print(f"    {fest['label']}: {len(idx)} shows in the programme")
        if idx:
            indexes.append((fest, idx))
    if not indexes:
        return {"matched": 0, "missed": len(pending)}

    matched = missed = 0
    per_festival: dict[str, int] = {}
    for row in pending[: limit or len(pending)]:
        # The programme frequently lists a show under its performer as well
        # ("Leo Hincks: Emotional Cowboy"), while publications review it by
        # title alone. Trying both costs nothing and is the difference between
        # a genre and a blank badge.
        candidates = [normalise(row["title"])]
        if row["performer"]:
            candidates.append(normalise(f"{row['performer']} {row['title']}"))
        hit = next(((f, i[k]) for k in candidates for f, i in indexes if k in i), None)
        key = candidates[0]
        if hit is None and row["edfringe_url"]:
            # Linked by the gate under a spelling the title alone does not
            # reproduce; the URL it found is still the right page to read.
            hit = next(((f, row["edfringe_url"]) for f in FESTIVALS
                        if f["key"] == row["festival"]), None)
        if hit is None:
            missed += 1
            continue

        fest, url = hit
        time.sleep(DELAY)
        html = _get(url)
        if html is None:
            missed += 1
            continue

        details = fest["details"](html)
        conn.execute(
            """UPDATE shows SET edfringe_url = ?, festival = ?, genre = ?,
                                 subgenre = ?, venue = ?, start_time = ?,
                                 duration = ? WHERE id = ?""",
            (url, fest["key"], details.get("genre", ""),
             details.get("subgenre", ""), details.get("venue", ""),
             details.get("start_time", ""), details.get("duration", ""),
             row["id"]),
        )
        matched += 1
        per_festival[fest["label"]] = per_festival.get(fest["label"], 0) + 1

    conn.commit()
    if per_festival:
        print("    linked: " + ", ".join(f"{v} {k}" for k, v in per_festival.items()))
    return {"matched": matched, "missed": missed}
