"""
Linking our shows to their official Fringe programme entries, and reading the
genre from them.

Two things come from the same place. The programme sitemap lists every show as
a slug derived from its title, so matching our titles against it gives us the
canonical URL to link to. Fetching that page then yields the Fringe's own
`genre` and `subGenre` fields — which is how we can say "Comedy / Sketch"
rather than guessing from a headline.

Only the current festival is enriched. Past years' shows are no longer sold, so
their programme pages disappear.
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

SITEMAP = "https://www.edfringe.com/tickets/sitemap.xml"
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


def programme_index() -> dict[str, str]:
    """
    {normalised title: programme URL} for every show in the current programme.

    The slug is generated from the show's title, so normalising it back gives a
    key our own titles can be matched against without fetching 4,000 pages.
    """
    raw = _get(SITEMAP, timeout=40)
    if raw is None:
        return {}

    index: dict[str, str] = {}
    for url in re.findall(r"<loc>(.*?)</loc>", raw):
        if "/whats-on/" not in url:
            continue
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        key = normalise(slug.replace("-", " "))
        if key:
            index.setdefault(key, url)
    return index


def show_details(url: str) -> dict | None:
    """Pull genre, subGenre and title out of a programme page's embedded JSON."""
    html = _get(url)
    if html is None:
        return None

    # The page is a Next.js app; the show record sits in the embedded props.
    m = re.search(r'"genre"\s*:\s*"([^"]*)"', html)
    if not m:
        return None
    sub = re.search(r'"subGenre"\s*:\s*"([^"]*)"', html)
    title = re.search(r'"title"\s*:\s*"([^"]{2,120})"', html)
    return {
        "genre": m.group(1).strip(),
        "subgenre": (sub.group(1).strip() if sub else ""),
        "title": (title.group(1).strip() if title else ""),
    }


def label(genre: str, subgenre: str) -> str:
    """
    What to print on the page.

    The Fringe's top-level genres are broad — everything from a stand-up hour to
    a sketch troupe is "COMEDY" — so the sub-genre is the informative half and
    goes first where it exists.
    """
    genre = (genre or "").title().replace("And", "&")
    if subgenre and subgenre.lower() not in genre.lower():
        return f"{genre} · {subgenre}"
    return genre


def enrich(conn: sqlite3.Connection, year: int, limit: int | None = None) -> dict[str, int]:
    """Attach programme URL and genre to this year's shows that lack them."""
    pending = conn.execute(
        """SELECT id, title FROM shows
            WHERE year = ? AND (edfringe_url IS NULL OR edfringe_url = '')
            ORDER BY title""",
        (year,),
    ).fetchall()
    if not pending:
        return {"matched": 0, "missed": 0}

    print(f"  fetching the {year} programme index")
    index = programme_index()
    print(f"    {len(index)} shows in the programme")
    if not index:
        return {"matched": 0, "missed": len(pending)}

    matched = missed = 0
    for row in pending[: limit or len(pending)]:
        url = index.get(normalise(row["title"]))
        if not url:
            missed += 1
            continue

        time.sleep(DELAY)
        details = show_details(url)
        if details is None:
            missed += 1
            continue

        conn.execute(
            "UPDATE shows SET edfringe_url = ?, genre = ?, subgenre = ? WHERE id = ?",
            (url, details["genre"], details["subgenre"], row["id"]),
        )
        matched += 1

    conn.commit()
    return {"matched": matched, "missed": missed}
