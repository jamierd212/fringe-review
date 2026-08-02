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

UA = {"User-Agent": "FringeLeaderboardBot/0.1 "
                    "(+https://github.com/jamierd212/fringe-review)"}
DELAY = 1.0          # edfringe.com's robots.txt asks for Crawl-delay: 1

# macOS Python ships without a usable system CA bundle, so verification fails
# with a bare urlopen. requests (used elsewhere) bundles certifi; here we pass
# it explicitly rather than the far worse option of disabling verification.
_SSL = ssl.create_default_context(cafile=certifi.where())


def _get(url: str, timeout: int = 25) -> str | None:
    try:
        req = urllib.request.Request(url, headers=UA)
        return urllib.request.urlopen(req, timeout=timeout,
                                      context=_SSL).read().decode("utf-8", "replace")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        print(f"      ! {type(exc).__name__} {url[:70]}")
        return None


def _fringe_details(html: str) -> dict:
    """The Fringe states genre and subGenre outright in the page JSON."""
    genre = re.search(r'"genre"\s*:\s*"([^"]*)"', html)
    sub = re.search(r'"subGenre"\s*:\s*"([^"]*)"', html)
    if not genre:
        return {}
    return {"genre": genre.group(1).strip(),
            "subgenre": (sub.group(1).strip() if sub else "")}


def _eif_details(html: str) -> dict:
    """
    EIF publishes no machine-readable genre — the words only appear inside prose
    descriptions, where "a night of dance and song" would be read as Dance for a
    concert. Rather than invent a classification and print it as fact, we record
    that the show is EIF and leave the genre empty.
    """
    return {"genre": "", "subgenre": ""}


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
]


def programme_index(festival: dict) -> dict[str, str]:
    """{normalised title: URL} for one festival's programme."""
    raw = _get(festival["sitemap"], timeout=40)
    if raw is None:
        return {}

    index: dict[str, str] = {}
    for url in re.findall(r"<loc>(.*?)</loc>", raw):
        if not any(p in url for p in festival["paths"]):
            continue
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        key = normalise(slug.replace("-", " "))
        if not key:
            continue
        index.setdefault(key, url)

        # EIF disambiguates some slugs with a trailing year ("inala-2026"), which
        # no review headline will carry. Index the bare form too so the show is
        # still findable, without letting it overwrite an exact match.
        stripped = re.sub(r"\s+(19|20)\d\d$", "", key)
        if stripped != key:
            index.setdefault(stripped, url)
    return index


def label(festival: str, genre: str, subgenre: str) -> str:
    """
    The badge text.

    Where a festival gives us a real classification we show it, because that is
    the informative part. Where it does not, the festival name still tells a
    reader something useful — that this is the International Festival rather
    than the Fringe.
    """
    genre = (genre or "").title().replace("And", "&")
    parts = [p for p in (genre, subgenre) if p]
    if not parts:
        return (festival or "").upper()
    return " · ".join(parts)


def enrich(conn: sqlite3.Connection, year: int, limit: int | None = None) -> dict[str, int]:
    """Attach programme URL, festival and genre to this year's unlinked shows."""
    pending = conn.execute(
        """SELECT id, title FROM shows
            WHERE year = ? AND (edfringe_url IS NULL OR edfringe_url = '')
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
        key = normalise(row["title"])
        hit = next(((f, i[key]) for f, i in indexes if key in i), None)
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
            """UPDATE shows SET edfringe_url = ?, festival = ?, genre = ?, subgenre = ?
                WHERE id = ?""",
            (url, fest["key"], details.get("genre", ""),
             details.get("subgenre", ""), row["id"]),
        )
        matched += 1
        per_festival[fest["label"]] = per_festival.get(fest["label"], 0) + 1

    conn.commit()
    if per_festival:
        print("    linked: " + ", ".join(f"{v} {k}" for k, v in per_festival.items()))
    return {"matched": matched, "missed": missed}
