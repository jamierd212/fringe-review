"""
Draw the day's top twenty as an image, and write the caption to go with it.

Instagram has no text-only post, so a leaderboard has to become a picture. The
card is 1080x1350 — the tallest crop Instagram shows without cutting, so twenty
rows fit at a size that can be read on a phone without opening the image.

Nothing is posted. The card and its caption are written to a folder for a person
to upload, which is also why the caption is a separate file: it is meant to be
copied.

The handles in the caption are only ever ones the company published on its own
programme entry. A mention reaches whoever holds that name, and congratulating
the wrong account in public cannot be undone.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "data" / "instagram"
WIDTH, HEIGHT = 1080, 1350
TOP = 20

# The site's own palette, so the card is recognisably the same thing.
BG = (249, 228, 224)
INK = (26, 26, 26)
MUTED = (107, 107, 107)
STAR = (198, 40, 40)
CARD = (255, 255, 255)

# Fonts are looked up by file because Pillow needs a path. Several candidates,
# because the machine that draws this is not necessarily a Mac.
FONTS = {
    "bold": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
             "/System/Library/Fonts/Supplemental/Arial Black.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "regular": ["/System/Library/Fonts/Supplemental/Arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
}


def _font(kind: str, size: int):
    for path in FONTS[kind]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    # Better a plain card than none: the default face is small and ugly, but the
    # numbers are still legible and the alternative is no card at all.
    return ImageFont.load_default(size)


def _fit(draw, text: str, font, room: int) -> str:
    """Shorten text with an ellipsis until it fits the width allowed."""
    if draw.textlength(text, font=font) <= room:
        return text
    while text and draw.textlength(text + "…", font=font) > room:
        text = text[:-1]
    return text.rstrip() + "…"


def draw_card(placed, when: date | None = None) -> Path:
    """Render the top twenty. Returns the path written."""
    when = when or date.today()
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    d = ImageDraw.Draw(img)

    d.text((60, 54), "EDINBURGH FESTIVALS", font=_font("bold", 34), fill=MUTED)
    d.text((60, 96), "Top 20", font=_font("bold", 76), fill=INK)
    d.text((60, 186), when.strftime("%-d %B %Y"), font=_font("regular", 30), fill=MUTED)
    # The ordering is by five- and four-star reviews, not by average, so the
    # average alone makes the card look mis-sorted: 5.0 sits below 4.9 and 3.8
    # above 4.4. Saying what ranks them, and showing how many reviews each
    # average rests on, is what makes the order legible.
    d.text((60, 226), "ranked by 5 and 4 star reviews",
           font=_font("regular", 26), fill=MUTED)

    top = 286
    row_h = (HEIGHT - top - 96) // TOP
    pos_font, title_font, rate_font = (_font("bold", 30), _font("bold", 30),
                                       _font("regular", 27))
    for index, (position, show) in enumerate(placed[:TOP]):
        y = top + index * row_h
        d.rounded_rectangle([48, y, WIDTH - 48, y + row_h - 8], radius=10, fill=CARD)
        d.text((72, y + 11), str(position), font=pos_font, fill=MUTED)
        rating = f"{show.mean:.1f} · {len(show.reviews)}"
        rating_w = d.textlength(rating, font=rate_font)
        # The title takes whatever the position and rating leave, and no more.
        title = _fit(d, show.title, title_font, WIDTH - 200 - rating_w - 96)
        d.text((146, y + 11), title, font=title_font, fill=INK)
        d.text((WIDTH - 72 - rating_w, y + 13), rating, font=rate_font, fill=STAR)

    d.text((60, HEIGHT - 74), "fringestars.com", font=_font("bold", 30), fill=INK)
    legend = "average · reviews"
    d.text((WIDTH - 60 - d.textlength(legend, font=_font("regular", 24)), HEIGHT - 70),
           legend, font=_font("regular", 24), fill=MUTED)

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"top20-{when.isoformat()}.png"
    img.save(path, "PNG", optimize=True)
    return path


def caption(conn: sqlite3.Connection, placed, when: date | None = None) -> str:
    """
    The caption: the twenty in order, with a mention where we have one.

    Instagram allows twenty mentions in a caption, which is exactly the number of
    shows here, so there is no room for the ones we would like to add and cannot.
    """
    when = when or date.today()
    handles = {
        row[0]: row[1]
        for row in conn.execute("SELECT show_id, handle FROM socials WHERE network = 'instagram'")
    }
    lines = [f"Edinburgh Festivals Top 20 — {when.strftime('%-d %B')}", ""]
    for position, show in placed[:TOP]:
        handle = handles.get(show.id)
        lines.append(f"{position}. {show.title} — {show.mean:.1f}"
                     + (f" @{handle}" if handle else ""))
    named = sum(1 for _, s in placed[:TOP] if s.id in handles)
    # Counted, not written down: the number of publications changes most weeks,
    # and a caption claiming last month's figure is a small untruth published
    # daily.
    publications = conn.execute(
        "SELECT COUNT(DISTINCT publication) FROM reviews").fetchone()[0]
    lines += ["",
              f"Ranked by every published review we can find, from "
              f"{publications} publications. Full board and all the reviews "
              f"at fringestars.com",
              "",
              "#edfringe #edinburghfringe #edfringe2026 #fringe #theatre #comedy"]
    text = "\n".join(lines)
    path = OUT / f"caption-{when.isoformat()}.txt"
    OUT.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return text, named
