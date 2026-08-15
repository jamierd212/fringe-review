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
    d.text((60, 226), "ranked by 5 and 4 star reviews",
           font=_font("regular", 26), fill=MUTED)

    top = 286
    row_h = (HEIGHT - top - 96) // TOP
    pos_font = _font("bold", 30)
    title_font = _font("bold", 30)
    meta_font = _font("regular", 23)
    for index, (position, show) in enumerate(placed[:TOP]):
        y = top + index * row_h
        d.rounded_rectangle([48, y, WIDTH - 48, y + row_h - 8], radius=10, fill=CARD)
        d.text((72, y + 11), str(position), font=pos_font, fill=MUTED)

        # Time and venue follow the name on the same line, in grey. The detail is
        # measured first and the title given what is left: the venue is the part
        # a reader acts on, so it keeps its room and the title gives way.
        detail = "  ·  ".join(x for x in (show.start_time, show.venue) if x)
        room = WIDTH - 146 - 92
        detail_w = d.textlength(detail, font=meta_font) if detail else 0
        title = _fit(d, show.title, title_font, room - detail_w - 28)
        d.text((146, y + 11), title, font=title_font, fill=INK)
        if detail:
            x = 146 + d.textlength(title, font=title_font) + 28
            d.text((x, y + 15), detail, font=meta_font, fill=(140, 140, 140))

    d.text((60, HEIGHT - 74), "fringestars.com", font=_font("bold", 30), fill=INK)

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
        lines.append(f"{position}. {show.title}"
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
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"caption-{when.isoformat()}.txt").write_text(text)

    # The handles on their own, in order, for tagging the photo in the app.
    # A caption mention notifies; a photo tag notifies AND puts the post in that
    # account's tagged feed, which is the more visible of the two and cannot be
    # automated without the Graph API. Having them as a list makes typing them
    # into "Tag people" a matter of copying twenty short strings rather than
    # picking them out of a paragraph.
    tags, missing = [], []
    for position, show in placed[:TOP]:
        handle = handles.get(show.id)
        (tags if handle else missing).append(
            f"@{handle}" if handle else f"{position}. {show.title}")
    lines = ["Tag these in the photo (Instagram allows 20):", ""] + tags
    if missing:
        lines += ["", "No handle on their programme entry — worth a look if you "
                      "want them tagged:", ""] + missing
    (OUT / f"tags-{when.isoformat()}.txt").write_text("\n".join(lines))
    return text, named
