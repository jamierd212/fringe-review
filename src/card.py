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

from PIL import Image, ImageChops, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "data" / "instagram"
LOGO = Path(__file__).resolve().parent.parent / "assets" / "logo.png"
WIDTH, HEIGHT = 1080, 1350
TOP = 20
LOGO_H = 190           # the height of the header block it sits beside
ROWS_TOP = 266         # where the first white box starts
# Fixed rather than derived from the space left over. Deriving it meant every
# trim to the header silently made the rows TALLER, spreading the white space
# rather than reducing it — the opposite of what shrinking the header is for.
ROW_H = 48
# How wide a show's name may run before it is cut. Set by eye, at the length of
# "Man Sings The Same Song Over And Over" — the longest title on the board and
# the one that decides where this needs to sit.
TITLE_MAX = 640
# Where a row's text starts and stops. The white card behind it runs 48 to 1032,
# so these leave 24px of padding at each end. They were 146 and 92, which was
# generous padding bought at the cost of the venue on the longest rows.
TEXT_L, TEXT_R = 134, 72

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


def _logo(img: Image.Image) -> int:
    """
    Place the logo in the top right corner. Returns the width it took, or 0.

    The width matters to the caller: the title is set on one line beside it, and
    has to know how much of the line the badge has already claimed.

    A missing logo is not an error. The card is drawn every morning by a machine
    nobody is watching, and a leaderboard without its badge is worth more than no
    leaderboard at all.

    Transparency matters here. A logo saved on its own flat square lands as a
    visible tile rather than a mark — and it does so even when that square is
    meant to match, because "the site pink" exported from a drawing tool is
    rarely the exact pink drawn here. The file supplied is (250, 224, 219)
    against a card of (249, 228, 224): invisible on screen, a clear rectangle
    once posted.

    So where the file has no alpha of its own, the background is knocked out by
    colour rather than by brightness — whatever the corner pixel is, near
    matches of it become transparent. Brightness alone would keep a pale pink
    and lose a pale yellow, and there is a pale yellow star in this one.

    The knockout ramps rather than switching, so anti-aliased edges keep their
    softness instead of gaining a hard fringe.
    """
    if not LOGO.exists():
        return 0
    logo = Image.open(LOGO).convert("RGBA")
    if min(logo.getchannel("A").get_flattened_data()) == 255:
        corner = logo.getpixel((0, 0))[:3]
        flat = Image.new("RGB", logo.size, corner)
        difference = ImageChops.difference(logo.convert("RGB"), flat).convert("L")
        logo.putalpha(Image.eval(difference, lambda v: min(255, max(0, (v - 6) * 10))))

    box = logo.getbbox() or (0, 0, *logo.size)     # drop the empty margin
    logo = logo.crop(box)
    scale = LOGO_H / logo.height
    logo = logo.resize((max(1, round(logo.width * scale)), LOGO_H), Image.LANCZOS)
    # Centred in the band above the first row, rather than hung from the top
    # margin. The header text is left-aligned and ragged; the badge is the only
    # thing on the right, so it has nothing to line up with and reads best
    # sitting in the middle of the space it has.
    img.paste(logo, (WIDTH - 60 - logo.width, (ROWS_TOP - LOGO_H) // 2), logo)
    return logo.width


def draw_card(placed, when: date | None = None) -> Path:
    """Render the top twenty. Returns the path written."""
    when = when or date.today()
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    d = ImageDraw.Draw(img)

    _logo(img)

    # Two lines, so the number keeps its size. On one line the whole title has
    # to shrink to clear the badge; stacked, only the qualifier is narrow enough
    # to need the room, and "Top 20" stays as large as the card allows.
    # Three lines on stated baselines, spread across the band above the first
    # row. Set from their tops as they were before, the gaps between them came
    # out at 4px and 3px while 68px sat empty underneath — the type was touching
    # itself at one end of the space and nowhere near the other. Baselines are
    # the only way to place lines of 34, 76 and 38 evenly, because each box
    # carries a different amount of air above and below its letters.
    number = _font("bold", 76)
    d.text((60, 73), "Edinburgh Festivals", font=_font("bold", 34), fill=MUTED,
           anchor="ls")
    d.text((60, 166), "Top 20", font=number, fill=INK, anchor="ls")
    # The date sits beside the number, on the same baseline, so two very
    # different sizes still read as one line.
    d.text((60 + d.textlength("Top 20", font=number) + 26, 166),
           when.strftime("%-d %B %Y"), font=_font("regular", 32),
           fill=MUTED, anchor="ls")
    d.text((60, 234), "Critically Reviewed Shows", font=_font("bold", 38),
           fill=INK, anchor="ls")

    top, row_h = ROWS_TOP, ROW_H
    pos_font = _font("bold", 30)
    # Regular, not bold. Twenty bold names down the card left nothing for the
    # header to be louder than; the position numbers hold the weight now and the
    # titles read as a list rather than twenty separate headlines.
    title_font = _font("regular", 30)
    meta_font = _font("regular", 23)
    for index, (position, show) in enumerate(placed[:TOP]):
        y = top + index * row_h
        d.rounded_rectangle([48, y, WIDTH - 48, y + row_h - 8], radius=10, fill=CARD)

        # Everything on the row hangs off the box's own centre line, set with
        # Pillow's middle anchor rather than by nudging each baseline. Three
        # sizes of type share this row; offsets tuned for one of them leave the
        # other two sitting low, which is what put the whole row against the
        # bottom edge before.
        mid = y + (row_h - 8) / 2
        d.text((72, mid), str(position), font=pos_font, fill=MUTED, anchor="lm")

        # The name on the left, venue and time in grey on the right, both
        # against their own margin. Ranged right, the detail forms its own
        # column down the card rather than starting at twenty different places.
        #
        # The title is capped because one allowed the full width would collide
        # with that column, and a name cut at a readable length costs less than
        # a row with nowhere to put its venue.
        title = _fit(d, show.title, title_font, TITLE_MAX)
        d.text((TEXT_L, mid), title, font=title_font, fill=INK, anchor="lm")

        # Where the two would meet, the venue goes before the time does: the
        # time is the shorter of the pair and the more use to somebody deciding
        # what to see tonight.
        limit = WIDTH - TEXT_R
        after_title = TEXT_L + d.textlength(title, font=title_font) + 28
        for detail in ("  ·  ".join(t for t in (show.venue, show.start_time) if t),
                       show.start_time or ""):
            width = d.textlength(detail, font=meta_font)
            if detail and limit - width >= after_title:
                d.text((limit, mid), detail, font=meta_font,
                       fill=(140, 140, 140), anchor="rm")
                break

    d.text((60, HEIGHT - 82), "fringestars.com", font=_font("bold", 42), fill=INK)

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
