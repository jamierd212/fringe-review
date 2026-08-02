"""Stage 5: turn the database into a single static HTML page."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db, rank
from .collect import load_config

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
OUTPUT = ROOT / "site" / "index.html"


def stars_html(n: int) -> str:
    """Five glyphs, n of them filled — so every row lines up vertically."""
    return "★" * n + "☆" * (5 - n)


def run(conn: sqlite3.Connection, year: int) -> Path:
    ranked, rest = rank.leaderboard(conn)
    placed = rank.positions(ranked)

    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["stars"] = stars_html

    now = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/London"))
    publications = sorted(
        {r["publication"] for r in conn.execute("SELECT DISTINCT publication FROM reviews")}
    )

    defaults = load_config().get("defaults", {})

    html = env.get_template("index.html.j2").render(
        placed=placed,
        rest=rest,
        year=year,
        updated=now.strftime("%-d %B %Y, %H:%M %Z"),
        stats=db.stats(conn),
        publications=publications,
        contact_url=defaults.get(
            "contact_url", "https://github.com/jamierd212/fringe-review/issues/new"
        ),
        contact_label=defaults.get("contact_label", "let us know"),
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html, encoding="utf-8")
    return OUTPUT
