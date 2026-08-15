"""
Draw the day's top twenty as an image, and write the caption to go with it.

Instagram has no text-only post, so a leaderboard has to become a picture. The
card is 1080x1280. Instagram's feed will not show anything taller than 4:5, and
1080x1350 is exactly 4:5 — sitting on the line, where any rounding in their
pipeline takes a slice off it. A little inside the limit is never cropped, and
twenty rows still fit at a size that can be read on a phone.

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
WIDTH, HEIGHT = 1080, 1280
TOP = 20
LOGO_H = 190           # the height of the header block it sits beside
ROWS_TOP = 266         # where the first white box starts
# Fixed rather than derived from the space left over. Deriving it meant every
# trim to the header silently made the rows TALLER, spreading the white space
# rather than reducing it — the opposite of what shrinking the header is for.
ROW_H = 46
# The band above the first row, and what it keeps clear at each end.
HEADER_TOP, HEADER_BOTTOM = 42, 23
# How wide a show's name may run before it is cut, at the length of "Man Sings
# The Same Song Over And Over" — the longest title on the board and the one that
# decides where this sits.
#
# It is measured against the weight the titles are actually set in. At 640 in
# bold that title stopped after "Over"; in regular, more characters fit the same
# 640, so it ran on to "Over A…" and shouldered Summerhall off the row. 630
# gives the same words back and the venue with them.
TITLE_MAX = 630
# Below this a title has given up enough, and the venue shortens instead.
TITLE_MIN = 420
# Where a row's text starts and stops. The white card behind it runs 48 to 1032,
# so these leave 24px of padding at each end. They were 146 and 92, which was
# generous padding bought at the cost of the venue on the longest rows.
TEXT_L, TEXT_R = 134, 72
# The least a shortened venue may be and still name somewhere. Below this it
# is dropped in favour of the time alone.
VENUE_MIN = 110
# The position number's right edge. The climb marker used to sit to its left,
# which put the one coloured thing on the card in the reader's path before they
# had read a single name. It now sits at the far right, after the venue.
POS_R = 120
# Green, for the one thing on the card that is good news.
RISE = (34, 139, 87)

# The site's own palette, so the card is recognisably the same thing.
BG = (249, 228, 224)
INK = (26, 26, 26)
MUTED = (107, 107, 107)
STAR = (198, 40, 40)
CARD = (255, 255, 255)

# Inter, carried in the repository rather than taken from the machine.
#
# The card is drawn by the nightly run on Ubuntu, not on the Mac it is designed
# on, so anything chosen from the system fonts here would silently fall back to
# DejaVu there and the posted card would not be the card anyone approved. A file
# in the repo renders the same in both places, which is the whole point.
#
# Inter is drawn for screens and holds its shape at the 23px the venue column
# runs at, where Arial starts to look like a spreadsheet. SIL Open Font Licence,
# so redistributing it here is permitted; the licence travels with it.
#
# InterDisplay is the same face cut for large sizes — tighter spacing, finer
# detail — and is used for the header alone, which is what it is for.
_FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
FONTS = {
    "bold": [_FONTS_DIR / "Inter-Bold.ttf",
             "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "display": [_FONTS_DIR / "InterDisplay-Bold.ttf",
                _FONTS_DIR / "Inter-Bold.ttf",
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "regular": [_FONTS_DIR / "Inter-Regular.ttf",
                "/System/Library/Fonts/Supplemental/Arial.ttf",
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
    """
    Shorten text with an ellipsis so that the RESULT fits the width allowed.

    The result, not the text before the ellipsis. An earlier version measured
    the string plus "…" and then returned it plus " …", so anything it shortened
    came back a space wider than it had promised. The caller checked the width
    it was given, found it over, and drew nothing at all — which is how a row
    lost its venue and its time together.

    Where a whole word can be given up and most of the room still used, it is:
    "Over and …" reads as a title carrying on, "Over and Ove…" as a fault.
    """
    if draw.textlength(text, font=font) <= room:
        return text

    def marked(stem: str) -> str:
        return stem + (" …" if " " in stem else "…")

    cut = ""
    for i in range(len(text), 0, -1):
        stem = text[:i].rstrip()
        if stem and draw.textlength(marked(stem), font=font) <= room:
            cut = stem
            break
    if not cut:
        return "…"
    if " " in cut:
        trimmed = cut[:cut.rindex(" ")].rstrip()
        if trimmed and draw.textlength(marked(trimmed), font=font) >= room * 0.8:
            cut = trimmed
    return marked(cut)


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


def _climb(previous: dict[str, int] | None, show, position: int) -> str | None:
    """
    How far this show has come, as it will be written, or None if it has not.

    Measured against every show's last position rather than only the twenty
    shown, so a show arriving from 45th can say so. A show with no previous
    position at all is new to the board entirely: it gets the arrow without a
    figure, because any number there would be invented.
    """
    if not previous:
        return None
    was = previous.get(show.id)
    if was is None:
        return ""
    return str(was - position) if was > position else None


def _rise(d: ImageDraw.ImageDraw, right: float, mid: float, places: str,
          font) -> float:
    """Draw the marker with its right edge at `right`. Returns its width."""
    w, h, gap = 12, 12, 3
    width = w + (gap + d.textlength(places, font=font) if places else 0)
    x = right - width
    d.polygon([(x + w / 2, mid - h / 2), (x, mid + h / 2), (x + w, mid + h / 2)],
              fill=RISE)
    if places:
        d.text((x + w + gap, mid), places, font=font, fill=RISE, anchor="lm")
    return width


def draw_card(placed, when: date | None = None,
              previous: dict[str, int] | None = None) -> Path:
    """
    Render the top twenty. Returns the path written.

    `previous` is where each show stood at the end of the last run, used to
    mark the ones that have climbed. Posting the same twenty names every
    morning gives a reader nothing to look for; an arrow gives them the
    day's news at a glance.

    Absent, nothing is marked. That is the honest state on the first run of a
    year, when every show is new and none of them has moved.
    """
    when = when or date.today()
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    d = ImageDraw.Draw(img)

    _logo(img)

    # The header, laid out rather than hand-placed. Three lines share the band
    # above the first row, and the gaps between them are computed from each
    # line's own ascent and descent so they come out equal on the page. Hand
    # baselines were right until a size changed, and then silently wrong: every
    # type size carries a different amount of air above and below its letters,
    # so numbers that look evenly spaced are not.
    lines = [("Edinburgh Festivals", "display", 38, MUTED),
             ("Top 20", "display", 76, INK),
             ("Critically Acclaimed Shows", "display", 38, INK)]
    fonts = [_font(kind, size) for _, kind, size, _ in lines]
    metrics = [f.getmetrics() for f in fonts]
    ink = sum(a + desc for a, desc in metrics)
    gap = (ROWS_TOP - HEADER_TOP - HEADER_BOTTOM - ink) / (len(lines) - 1)

    y = HEADER_TOP
    for (text, _, _, colour), font, (ascent, descent) in zip(lines, fonts, metrics):
        baseline = y + ascent
        d.text((60, baseline), text, font=font, fill=colour, anchor="ls")
        # The date rides on the number's baseline, set as the line above it so
        # the two read as one label rather than two decisions.
        if text == "Top 20":
            d.text((60 + d.textlength(text, font=font) + 26, baseline),
                   when.strftime("%-d %B %Y"), font=fonts[0], fill=MUTED,
                   anchor="ls")
        y = baseline + descent + gap

    top, row_h = ROWS_TOP, ROW_H
    pos_font = _font("bold", 30)
    climb_font = _font("bold", 18)

    # How much of the right edge the markers need TODAY, rather than the most
    # they could ever need. A day with no climbers reserves nothing and the
    # venues get the full width back; a day whose biggest jump is "3" reserves
    # room for "3". Computed across the whole card, not per row, so the detail
    # column still lines up down the page.
    climbs = {show.id: _climb(previous, show, position)
              for position, show in placed[:TOP]}
    marker_w = max((12 + (3 + d.textlength(c, font=climb_font) if c else 0)
                    for c in climbs.values() if c is not None), default=0)
    # Inside the boxes, at their right edge. Out in the margin the markers
    # were tidier in principle and worse on the page: the boxes had to narrow
    # to make room, so the list ended on a ragged edge against a column that is
    # empty on most rows.
    box_r = WIDTH - 48
    detail_r = WIDTH - TEXT_R - (marker_w + 16 if marker_w else 0)
    # Regular, not bold. Twenty bold names down the card left nothing for the
    # header to be louder than; the position numbers hold the weight now and the
    # titles read as a list rather than twenty separate headlines.
    title_font = _font("regular", 30)
    meta_font = _font("regular", 23)
    for index, (position, show) in enumerate(placed[:TOP]):
        y = top + index * row_h
        d.rounded_rectangle([48, y, box_r, y + row_h - 8], radius=10, fill=CARD)

        # Everything on the row hangs off the box's own centre line, set with
        # Pillow's middle anchor rather than by nudging each baseline. Three
        # sizes of type share this row; offsets tuned for one of them leave the
        # other two sitting low, which is what put the whole row against the
        # bottom edge before.
        mid = y + (row_h - 8) / 2
        # Ranged right, so 1 and 20 share an edge instead of both starting at
        # the same place and looking staggered.
        d.text((POS_R, mid), str(position), font=pos_font, fill=MUTED, anchor="rm")

        # The name on the left, venue and time in grey on the right, both
        # against their own margin. Ranged right, the detail forms its own
        # column down the card rather than starting at twenty different places.
        #
        # The title is capped because one allowed the full width would collide
        # with that column, and a name cut at a readable length costs less than
        # a row with nowhere to put its venue.
        # The title takes its cap, and the venue shortens to fit around it —
        # that order keeps a name whole wherever a shortened venue can still
        # say something useful.
        #
        # Only where even a minimum venue will not fit does the title give way
        # instead, two words at a time. That is one row on the board: the
        # longest title, which was taking the whole line and leaving its venue
        # nowhere to go. "Over and …" beside "Summerhall" beats the full name
        # beside nothing.
        venue, clock = show.venue or "", show.start_time or ""
        detail = "  ·  ".join(t for t in (venue, clock) if t)
        tail = f"  ·  {clock}" if clock else ""
        floor = VENUE_MIN + d.textlength(tail, font=meta_font)

        title = _fit(d, show.title, title_font, TITLE_MAX)
        if venue and detail_r - (TEXT_L + d.textlength(title, font=title_font) + 28) < floor:
            title = _fit(d, show.title, title_font,
                         max(TITLE_MIN, detail_r - floor - 28 - TEXT_L))
        d.text((TEXT_L, mid), title, font=title_font, fill=INK, anchor="lm")

        # Where the two would meet, the venue gives way — but by being shortened
        # rather than dropped. "Assembly George Sq…" still tells a reader which
        # side of town to walk to; nothing at all tells them less than the row
        # above with a venue on it, and reads like a mistake beside it.
        #
        # Below a working minimum it is dropped after all: three letters and an
        # ellipsis name no venue in Edinburgh, and the time is worth more than a
        # stub.
        limit = detail_r
        after_title = TEXT_L + d.textlength(title, font=title_font) + 28
        room = limit - after_title
        if venue and d.textlength(detail, font=meta_font) > room:
            spare = room - d.textlength(tail, font=meta_font)
            detail = (_fit(d, venue, meta_font, spare) + tail
                      if spare >= VENUE_MIN else clock)
        if detail and d.textlength(detail, font=meta_font) <= room:
            d.text((limit, mid), detail, font=meta_font,
                   fill=(140, 140, 140), anchor="rm")

        # The climb marker last, hard against the right margin, so the arrows
        # form their own column clear of everything else.
        climbed = climbs.get(show.id)
        if climbed is not None:
            _rise(d, WIDTH - TEXT_R, mid, climbed, climb_font)

    d.text((60, HEIGHT - 82), "fringestars.com", font=_font("display", 42), fill=INK)

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"top20-{when.isoformat()}.png"
    img.save(path, "PNG", optimize=True)
    return path


VENUE_SOCIALS = Path(__file__).resolve().parent.parent / "data" / "venue-socials.json"
# Instagram's own limit on how many accounts one photo may tag.
TAG_LIMIT = 20


def _venue_tags(placed, taken: set[str], slots: int) -> list[tuple[str, str]]:
    """
    Venue handles to fill the spare tag slots, busiest first.

    Shows come first because they are what the post is about, but a card rarely
    has twenty handles — the companies that list one are a subset — and an
    untagged slot is wasted reach. Venues have far larger followings than the
    acts they house, so the leftovers are worth more spent there.

    Matched on the start of the programme's venue name, so "Assembly Roxy" and
    "Assembly @ St Andrew Square" both reach Assembly and count as one tag
    rather than three. Handles come from each venue's own website; a venue we
    have no published handle for is left alone rather than guessed at.
    """
    import json
    from collections import Counter

    if slots <= 0 or not VENUE_SOCIALS.exists():
        return []
    known = {k: v for k, v in json.loads(VENUE_SOCIALS.read_text()).items()
             if not k.startswith("_")}
    counts: Counter = Counter()
    for _position, show in placed[:TOP]:
        name = show.venue or ""
        match = next((k for k in known if name.startswith(k)), None)
        if match and known[match].lower() not in taken:
            counts[match] += 1
    return [(venue, known[venue]) for venue, _n in counts.most_common(slots)]


def caption(conn: sqlite3.Connection, placed, when: date | None = None,
            peaks: set[str] | None = None) -> str:
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
    # Only shows that have just gone higher than they have ever been. The board
    # barely moves from one morning to the next, so tagging all twenty daily
    # says "you are still there" — not news, and the surest way to be muted by
    # the accounts most worth reaching. `peaks` is None only when nothing has
    # been recorded yet, in which case everyone is at their best by definition.
    worth_telling = [(position, show) for position, show in placed[:TOP]
                     if peaks is None or show.id in peaks]
    tags, missing = [], []
    for position, show in worth_telling:
        handle = handles.get(show.id)
        (tags if handle else missing).append(
            f"@{handle}" if handle else f"{position}. {show.title}")

    # Spare slots go to the venues carrying the most of today's twenty. Photo
    # tags are hidden until someone taps the image, so these cost the caption
    # nothing while reaching an audience far larger than most of the acts have.
    venues = _venue_tags(placed, {t.lstrip("@").lower() for t in tags},
                         TAG_LIMIT - len(tags))
    lines = [f"Tag these in the photo ({len(tags) + len(venues)} of "
             f"{TAG_LIMIT} Instagram allows):", "",
             "Shows at their highest position yet:", ""] + (tags or ["(none today)"])
    if venues:
        lines += ["", "Venues, filling the spare slots:", ""] + [
            f"@{handle}   ({venue})" for venue, handle in venues]
    if missing:
        lines += ["", "At a new high but no handle on their programme entry:",
                  ""] + missing
    (OUT / f"tags-{when.isoformat()}.txt").write_text("\n".join(lines))
    return text, named
