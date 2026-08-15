"""Stage 5: turn the database into one static HTML page per festival year."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import genres, programme, rank
from .collect import load_config

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
OUTPUT_DIR = ROOT / "docs"

# Used for canonical URLs and the sitemap. A custom domain only needs changing
# here — nothing else in the codebase hard-codes the site address.
SITE_URL = "https://fringestars.com"

# Venue -> geographic group, built from the festivals' own venue pages by
# tools/venue_groups.py. Kept as data rather than derived at render time: it
# needs 300 requests to rebuild and changes once a year, not once a day.
VENUE_GROUPS = json.loads((ROOT / "data" / "venue-groups.json").read_text()) \
    if (ROOT / "data" / "venue-groups.json").exists() else {}

# How many shows the festival programme lists in each area. Used to order the
# filter, because ordering by the shows WE have reviewed is unstable and tiny —
# an area with two reviews would outrank the Royal Mile.
AREA_SHOWS = json.loads((ROOT / "data" / "area-shows.json").read_text()) \
    if (ROOT / "data" / "area-shows.json").exists() else {}

# Deliberate exceptions to that ordering. Summerhall has only 96 shows against
# the Royal Mile's 1,422, but it is a destination people choose on purpose
# rather than somewhere they happen to be, so burying it eleventh on raw counts
# reads wrong. An editorial override, kept here where it is visible rather than
# hidden in the sort.
PINNED_AFTER = {"Summerhall & The Meadows": "George Square"}


def playing_days(conn: sqlite3.Connection, year: int) -> dict[str, str]:
    """
    For each show, the days it can still be booked, as offsets from 1 August.

    Offsets rather than dates because this ends up in an attribute on every row
    on the page: "7 8 9" instead of "2026-08-07,2026-08-08,2026-08-09" is a
    third of the bytes across eight hundred shows, and the browser has to do
    arithmetic on them either way.

    Only performances the festival has not called off. Cancelled and sold-out
    dates are left out entirely rather than marked, because a filter's job here
    is to answer "can I go", and a date that cannot be attended is not an
    answer to it.
    """
    from datetime import date as _date

    base = _date(year, 8, 1).toordinal()
    days: dict[str, set[int]] = {}
    try:
        rows = conn.execute(
            """SELECT p.show_id, p.date FROM performances p
                 JOIN shows s ON s.id = p.show_id
                WHERE s.year = ? AND p.status IN (%s)"""
            % ",".join("?" * len(programme.BOOKABLE)),
            (year, *programme.BOOKABLE),
        )
    except sqlite3.OperationalError:
        # No calendars collected yet. The filter hides itself rather than
        # offering a choice that would empty the page.
        return {}
    for show_id, when in rows:
        try:
            offset = _date(*map(int, when.split("-"))).toordinal() - base
        except (TypeError, ValueError):
            continue
        days.setdefault(show_id, set()).add(offset)
    return {k: " ".join(str(n) for n in sorted(v)) for k, v in days.items()}


def order_areas(areas: set[str]) -> list[str]:
    """Areas by programme size, then the pinned exceptions moved into place."""
    ordered = sorted(areas, key=lambda a: (-AREA_SHOWS.get(a, 0), a.casefold()))
    for area, after in PINNED_AFTER.items():
        if area in ordered and after in ordered:
            ordered.remove(area)
            ordered.insert(ordered.index(after) + 1, area)
    return ordered

FESTIVAL_NAMES = {"fringe": "Fringe", "eif": "Edinburgh International Festival",
                  "freefringe": "Free Fringe", "art": "Art Festival"}


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
    # The real calendar year ALWAYS gets a page, whatever this run was scoped
    # to. Deriving "current" from the caller's year meant `--backfill 2025-08
    # --render` treated 2025 as current, so 2026 was not in `available` and the
    # stale-page prune deleted the live 2026 board — the page the domain serves.
    for candidate in (datetime.now().year, year):
        if candidate and candidate not in available:
            available.append(candidate)
    available.sort(reverse=True)
    # Land on the newest year with content; fall back to the newest year at all.
    landing = landing_year(populated, available)

    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["stars"] = stars_html
    env.globals["venue_group"] = lambda v: VENUE_GROUPS.get(v or "", "")
    env.globals["genre_keys"] = lambda s: genres.keys_for(
        getattr(s, "festival", ""), getattr(s, "section", ""),
        getattr(s, "subgenre", ""))
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

        # Only the current festival has a live programme, so past years have no
        # venues to offer and the filter is left out of those pages entirely.
        venues = sorted({s.venue for s in ranked + rest if s.venue}, key=str.casefold)
        # Only offer an area that actually contains a show this year: picking
        # "Leith" and getting nothing is worse than not being offered Leith.
        # Ordered by how much is ON in each area according to the programme,
        # not by how much we have reviewed. Reviews arrive in a trickle and
        # would reorder the list daily; the programme is the stable answer to
        # "where is the most happening", which is what someone scanning wants.
        groups = order_areas({VENUE_GROUPS[v] for v in venues if v in VENUE_GROUPS})
        # 09:00 through to 08:00 the next morning: a festival day, in the order
        # someone lives it, rather than a clock starting at midnight.
        hours = [f"{(9 + i) % 24:02d}:00" for i in range(24)]
        genre_sections, genre_subs = genres.options(ranked + rest)
        playing = playing_days(conn, this_year)

        def build(canonical: str) -> str:
            return template.render(
                canonical=canonical,
                placed=rank.placement(conn, ranked, this_year),
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
                analytics_token=defaults.get("analytics_token", ""),
                venues=venues,
                venue_groups=groups,
                genre_sections=genre_sections,
                genre_subs=genre_subs,
                hours=hours,
                playing=playing,
                playing_epoch=f"{this_year}-08-01",
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
                analytics_token=defaults.get("analytics_token", ""),
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

    _write_bot_page(defaults)
    _write_sitemap(sitemap, now)
    return written


def _write_bot_page(defaults: dict) -> None:
    """
    What the crawler is, for whoever looks up the User-Agent.

    Deliberately not linked from the site: its readers arrive from a server log,
    not from the leaderboard. It answers the three questions a sysadmin actually
    has - who is this, how hard are they hitting me, and how do I stop them.
    """
    agent = defaults.get("user_agent", "FringeLeaderboardBot/0.1")
    contact = defaults.get("contact_url", "mailto:corrections@fringestars.com")
    label = defaults.get("contact_label", "get in touch")
    (OUTPUT_DIR / "bot.html").write_text(f"""<!DOCTYPE html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>About FringeLeaderboardBot</title>
<style>
  body {{ margin: 0; padding: 2rem 1rem 4rem; background: #f9e4e0; color: #1a1a1a;
         font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               "Helvetica Neue", Arial, sans-serif; }}
  main {{ max-width: 40rem; margin: 0 auto; background: #fff; border: 1px solid #ecdcd9;
          border-radius: 6px; padding: 1.5rem; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 1rem; }}
  h2 {{ font-size: 1rem; margin: 1.6rem 0 .4rem; }}
  code {{ background: #f6f6f6; padding: .1rem .3rem; border-radius: 3px; font-size: .9em; }}
  a {{ color: inherit; }}
</style>
</head>
<body>
<main>
<h1>FringeLeaderboardBot</h1>

<p>If you have seen <code>{agent}</code> in your
server logs, this is us.</p>

<p>It collects <strong>star ratings</strong> for Edinburgh festival shows and
collates them into a leaderboard at <a href="{SITE_URL}/">fringestars.com</a>.
Every rating is attributed to the publication that gave it and links straight
back to the review on your own site. We publish no review text, no extracts and
no images.</p>

<h2>How it behaves</h2>
<ul>
  <li>At most one request per second, and only to pages we have reason to think
      are reviews.</li>
  <li>It reads <code>robots.txt</code> and obeys it.</li>
  <li>It runs once a day, and only during the festival season for most sources.</li>
  <li>If your server refuses us, we stop. We do not disguise the user-agent or
      work around a block.</li>
</ul>

<h2>If you would rather we did not</h2>
<p>Add this to your <code>robots.txt</code> and we will stop on the next run:</p>
<p><code>User-agent: FringeLeaderboardBot<br>Disallow: /</code></p>
<p>Or <a href="{contact}">{label}</a> and we will remove you by hand, along with
any ratings of yours already published. No explanation needed.</p>

<h2>Corrections</h2>
<p>If a rating of yours is recorded wrongly,
<a href="{contact}">{label}</a> and it will be fixed.</p>
</main>
</body>
</html>
""", encoding="utf-8")


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
