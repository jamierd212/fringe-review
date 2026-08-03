"""Stage 5: turn the database into one static HTML page per festival year."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import rank
from .collect import load_config

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
OUTPUT_DIR = ROOT / "docs"

# Used for canonical URLs and the sitemap. A custom domain only needs changing
# here — nothing else in the codebase hard-codes the site address.
SITE_URL = "https://fringestars.com"

FESTIVAL_NAMES = {"fringe": "Fringe", "eif": "Edinburgh International Festival",
                  "freefringe": "Free Fringe", "art": "Art Festival",
                  "book": "Book Festival"}


def show_jsonld(show, year: int, festival_name: str) -> str:
    """
    schema.org markup so search engines can show the star rating in results.

    Every rating carries its own publication as the review author, which is both
    honest and what Google requires: these are third-party critics' verdicts
    being collated, not our own opinion of the show. The aggregate is displayed
    on the page too, because markup that claims something the reader cannot see
    is a guidelines breach.
    """
    data: dict = {
        "@context": "https://schema.org",
        "@type": "TheaterEvent",
        "name": show.title,
        "url": f"{SITE_URL}/{show.page}",
        "startDate": f"{year}-08",
        "location": {"@type": "Place", "name": f"Edinburgh {festival_name}",
                     "address": "Edinburgh, Scotland"},
    }
    if show.performer:
        data["performer"] = {"@type": "PerformingGroup", "name": show.performer}
    if show.average:
        data["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": show.average,
            "reviewCount": show.total,
            "bestRating": 5,
            "worstRating": 1,
        }
    data["review"] = [
        {
            "@type": "Review",
            "author": {"@type": "Organization", "name": r.publication},
            "reviewRating": {"@type": "Rating", "ratingValue": r.stars,
                             "bestRating": 5, "worstRating": 1},
            "url": r.url,
        }
        for r in show.reviews
    ]
    return json.dumps(data, indent=2, ensure_ascii=False)


def stars_html(n: int) -> str:
    """Five glyphs, n of them filled — so every row lines up vertically."""
    return "★" * n + "☆" * (5 - n)


def page_name(year: int) -> str:
    """
    Every year has one permanent URL, and it never moves.

    index.html is a COPY of whichever year we want people to land on, not the
    home of that year. An earlier version moved the landing year to index.html,
    which meant 2025 lived at /2025.html until it became the landing year, then
    at / — so any link anyone had already shared broke, and the file left behind
    at the old name went stale and kept serving wrong navigation.
    """
    return f"{year}.html"


def landing_year(populated: list[int], available: list[int]) -> int:
    """
    The year the bare domain shows: the newest one that actually HAS reviews.

    Before a festival opens, its board is an empty placeholder — sending everyone
    arriving at fringestars.com to a page with nothing on it wastes the visit.
    This flips to the new festival by itself the moment the first review lands,
    with no code change and nothing to remember to switch over.
    """
    return populated[0] if populated else available[0]


def run(conn: sqlite3.Connection, year: int | None = None) -> list[Path]:
    # The current festival always gets a page, even before it has any reviews —
    # otherwise index.html silently becomes last year's board, and anyone
    # arriving at the bare domain during festival week sees 2025.
    populated = rank.years(conn)
    available = list(populated)
    current = year or datetime.now().year
    if current not in available:
        available.append(current)
    available.sort(reverse=True)
    # Land on the newest year with content; fall back to the newest year at all.
    landing = landing_year(populated, available)

    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["stars"] = stars_html
    template = env.get_template("index.html.j2")

    now = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/London"))
    defaults = load_config().get("defaults", {})
    nav = [{"year": y, "href": page_name(y)} for y in available]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / ".nojekyll").touch()

    # Year pages left behind by an earlier run — a year that dropped out of the
    # data, or a file written under a naming scheme we no longer use. Left in
    # place they keep serving stale content and stale navigation.
    keep = {page_name(y) for y in available} | {"index.html"}
    for stale in OUTPUT_DIR.glob("[0-9][0-9][0-9][0-9].html"):
        if stale.name not in keep:
            stale.unlink()

    show_tpl = env.get_template("show.html.j2")
    written: list[Path] = []
    rendered_shows = []
    sitemap: list[str] = [SITE_URL + "/"]

    for this_year in available:
        ranked, rest = rank.leaderboard(conn, this_year)

        publications = sorted({
            r["publication"]
            for r in conn.execute(
                """SELECT DISTINCT r.publication FROM reviews r
                     JOIN shows s ON s.id = r.show_id
                    WHERE s.year = ? AND r.stars IS NOT NULL""",
                (this_year,),
            )
        })
        rated = conn.execute(
            """SELECT COUNT(*) FROM reviews r JOIN shows s ON s.id = r.show_id
                WHERE s.year = ? AND r.stars IS NOT NULL""",
            (this_year,),
        ).fetchone()[0]

        def build(canonical: str) -> str:
            return template.render(
                canonical=canonical,
                placed=rank.positions(ranked),
                rest=rest,
                year=this_year,
                nav=[dict(n, current=(n["year"] == this_year)) for n in nav],
                updated=now.strftime("%-d %B %Y, %H:%M %Z"),
                rated=rated,
                show_count=len(ranked) + len(rest),
                publications=publications,
                contact_url=defaults.get(
                    "contact_url",
                    "mailto:corrections@fringestars.com",
                ),
                contact_label=defaults.get("contact_label", "let us know"),
            )

        # The landing year is served at two URLs — its own page and the bare
        # domain. Both name the domain as canonical so search engines rank one
        # page rather than splitting the same content across two.
        is_landing = this_year == landing
        canonical = SITE_URL + "/" if is_landing else f"{SITE_URL}/{page_name(this_year)}"

        path = OUTPUT_DIR / page_name(this_year)
        path.write_text(build(canonical), encoding="utf-8")
        written.append(path)
        if is_landing:
            (OUTPUT_DIR / "index.html").write_text(build(canonical), encoding="utf-8")
        else:
            sitemap.append(canonical)

        # One page per show. This is the bulk of the site's searchable surface:
        # a leaderboard ranks 800 shows on two pages, but nobody searches for
        # "leaderboard" — they search for a show's name. It also gives a
        # performer something of their own to share.
        rendered_shows.extend(ranked + rest)
        for show in ranked + rest:
            festival_name = FESTIVAL_NAMES.get(
                conn.execute("SELECT festival FROM shows WHERE id = ?",
                             (show.id,)).fetchone()["festival"] or "", "Fringe")
            page = show_tpl.render(
                show=show, year=this_year, festival_name=festival_name,
                site_url=SITE_URL, back_href="../../" + page_name(this_year),
                jsonld=show_jsonld(show, this_year, festival_name),
                contact_url=defaults.get(
                    "contact_url",
                    "mailto:corrections@fringestars.com"),
                contact_label=defaults.get("contact_label", "let us know"),
            )
            out = OUTPUT_DIR / show.page / "index.html"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(page, encoding="utf-8")
            sitemap.append(f"{SITE_URL}/{show.page}")

    # Show pages for shows that no longer exist — a year removed from the data,
    # or an entry deleted as junk. Nothing overwrites these, so left alone they
    # stay live and reachable forever, and the sitemap stops being the only
    # place the site disagrees with itself.
    live = {(OUTPUT_DIR / s.page).resolve() for s in rendered_shows}
    show_root = OUTPUT_DIR / "show"
    if show_root.is_dir():
        for stale in show_root.iterdir():
            if stale.is_dir() and stale.resolve() not in live:
                shutil.rmtree(stale)

    _write_sitemap(sitemap, now)
    return written


def _write_sitemap(urls: list[str], now) -> None:
    """A sitemap so every show page gets discovered rather than waiting to be crawled."""
    stamp = now.date().isoformat()
    body = "\n".join(
        f"  <url><loc>{u}</loc><lastmod>{stamp}</lastmod></url>" for u in urls
    )
    (OUTPUT_DIR / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{body}\n</urlset>\n", encoding="utf-8")
    (OUTPUT_DIR / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n", encoding="utf-8")
