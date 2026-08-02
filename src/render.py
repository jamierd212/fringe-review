"""Stage 5: turn the database into one static HTML page per festival year."""

from __future__ import annotations

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


def stars_html(n: int) -> str:
    """Five glyphs, n of them filled — so every row lines up vertically."""
    return "★" * n + "☆" * (5 - n)


def page_name(year: int, newest: int) -> str:
    """
    The newest year lives at index.html so the bare URL always shows the current
    festival. Older years get their own page, which also means last year's
    leaderboard keeps a stable link once a new festival starts.
    """
    return "index.html" if year == newest else f"{year}.html"


def run(conn: sqlite3.Connection, year: int | None = None) -> list[Path]:
    available = rank.years(conn) or [year or datetime.now().year]
    newest = available[0]

    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["stars"] = stars_html
    template = env.get_template("index.html.j2")

    now = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/London"))
    defaults = load_config().get("defaults", {})
    nav = [{"year": y, "href": page_name(y, newest)} for y in available]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / ".nojekyll").touch()

    written: list[Path] = []
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

        html = template.render(
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
                "https://github.com/jamierd212/fringe-review/issues/new",
            ),
            contact_label=defaults.get("contact_label", "let us know"),
        )

        path = OUTPUT_DIR / page_name(this_year, newest)
        path.write_text(html, encoding="utf-8")
        written.append(path)

    return written
