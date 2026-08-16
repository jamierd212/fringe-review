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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# The Fringe publishes performance times in UTC; its own pages, and every
# poster and ticket in the city, show them on an Edinburgh clock.
EDINBURGH = ZoneInfo("Europe/London")
from urllib.error import HTTPError, URLError

import certifi

from .normalise import normalise

def _user_agent() -> dict:
    """
    The configured User-Agent, so there is exactly one of them.

    This module used to hold its own copy of the string. Two copies drift, and a
    crawler that identifies itself differently depending on which part of the
    code is running is worse than one that does not identify itself at all.
    """
    from .collect import load_config
    return {"User-Agent": load_config().get("defaults", {}).get(
        "user_agent", "FringeLeaderboardBot/0.1")}


UA = _user_agent()
DELAY = 1.0          # edfringe.com's robots.txt asks for Crawl-delay: 1

# macOS Python ships without a usable system CA bundle, so verification fails
# with a bare urlopen. requests (used elsewhere) bundles certifi; here we pass
# it explicitly rather than the far worse option of disabling verification.
_SSL = ssl.create_default_context(cafile=certifi.where())


_GATE = None


def _allowed(url: str) -> bool:
    """
    Consult the same robots.txt gate the collector uses.

    This module fetches festival sitemaps and show pages through its own
    urlopen, so without this it was the one part of the crawler exempt from the
    rules the site publicly promises to follow. All four festival sites permit
    us today; the point is that they would still be asked tomorrow.
    """
    global _GATE
    if _GATE is None:
        from .collect import Collector, load_config
        _GATE = Collector(load_config())
    return _GATE.allowed(url)


def _get(url: str, timeout: int = 25) -> str | None:
    if not _allowed(url):
        print(f"      ! robots.txt disallows  {url[:70]}")
        return None
    try:
        req = urllib.request.Request(url, headers=UA)
        return urllib.request.urlopen(req, timeout=timeout,
                                      context=_SSL).read().decode("utf-8", "replace")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        print(f"      ! {type(exc).__name__} {url[:70]}")
        return None


def _no_details(html: str) -> dict:
    """
    For festivals that publish no machine-readable classification.

    Inferring "Comedy" from a prose description is guesswork, and guesswork
    presented as the festival's own label is worse than no label at all.
    """
    return {}


def _json_array(html: str, key: str):
    """
    The array under `key` in the page JSON, matched by balancing brackets.

    A non-greedy regex stops at the first "]", which inside performances is the
    end of a nested list rather than the end of the array — so it returned a
    fragment that would not parse.
    """
    import json

    start = html.find(f'"{key}"')
    if start == -1:
        return None
    open_at = html.find("[", start)
    if open_at == -1:
        return None
    depth, i = 0, open_at
    while i < len(html):
        if html[i] == "[":
            depth += 1
        elif html[i] == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    try:
        return json.loads(html[open_at:i + 1])
    except ValueError:
        return None


def _local_clock(stamp: str) -> str:
    """
    "2026-08-05T20:30:00.000Z" -> "21:30", the time on an Edinburgh clock.

    The Fringe states performance times in UTC, with the Z to say so, while its
    own pages show them in local time. Reading the characters straight out of
    the string gave 20:30 for a show that starts at 21:30, and did it to every
    show on the board, because the festival runs in August and August is BST.
    """
    try:
        when = datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return ""
    if stamp.endswith("Z") or stamp[19:].startswith("+00"):
        when = when.replace(tzinfo=timezone.utc).astimezone(EDINBURGH)
    return when.strftime("%H:%M")


PERFORMANCES_SCHEMA = """
CREATE TABLE IF NOT EXISTS performances (
    show_id TEXT NOT NULL,
    date    TEXT NOT NULL,          -- YYYY-MM-DD on an Edinburgh clock
    time    TEXT NOT NULL,          -- HH:MM likewise
    status  TEXT NOT NULL,          -- the festival's own ticketStatus
    PRIMARY KEY (show_id, date, time)
);
CREATE INDEX IF NOT EXISTS performances_date ON performances (date);
CREATE TABLE IF NOT EXISTS performance_checks (
    show_id    TEXT PRIMARY KEY,
    checked_at TEXT NOT NULL
);
"""

# What the festival says about a performance. This is the field behind the
# calendar on a show's own page — the one that tells a reader which dates they
# can book — so storing it is storing that calendar.
#
# The complete set, read out of the site's own bundle rather than guessed at
# from a sample:
#
#   CANCELLED  EVENT_SPECIFIC  FREE_NON_TICKETED  FREE_TICKETED
#   NO_ALLOCATION_CONTACT_VENUE  PREVIEW_SHOW  TICKETS_AVAILABLE  TWO_FOR_ONE
#
# There is no sold-out value in it. A date is bookable, bookable through the
# venue, free, or cancelled; "the last seat has gone" is not a state this
# programme can express. The separate soldOut boolean was false on all 1,400
# performances sampled, as was ticketsAvailable — including on performances the
# same record marked TICKETS_AVAILABLE — so neither is read. soldOut is still
# honoured if it ever turns true, which costs nothing and covers the case where
# it means something we have not seen.
#
# NO_ALLOCATION_CONTACT_VENUE is the one that misleads, and it misled us. The
# name reads like "the venue sells these, ask them" — but the festival's own
# calendar legend renders it "No allocation remaining", in RED, alongside
# Cancelled. It means the box office has none left. It is the sold-out signal,
# and the enum simply does not use the words.
#
# Read the wrong way round it inverts the whole filter, which is what it did
# here: sold-out dates were being counted as available.
#
# It does conflate two situations, and the data cannot fully separate them. A
# show the festival never held an allocation for reads red on every date from
# the day it goes on sale, the same as one that has sold out. Where a show is
# red on some dates and cyan on others, the red ones have gone; where it is red
# throughout, the tickets were probably never the festival's to sell. Either
# way the reader cannot buy that date here, which is the question being asked.
CANCELLED = "CANCELLED"
VIA_VENUE = "NO_ALLOCATION_CONTACT_VENUE"
# Dates the festival will actually sell a ticket for. VIA_VENUE is NOT among
# them, and this was argued round in a circle before it settled.
#
# The festival shows those dates in red, labelled "No allocation remaining", and
# its message points the reader at the venue box office. So a red date is not
# provably sold out — the venue may have seats — but it is provably not on sale
# here, and a filter headed "tickets available" that returns them is wrong in
# the direction that wastes somebody's evening.
#
# Excluding them was tried once before and hid the number one show from the
# board entirely. That objection has gone: the leaderboard no longer filters by
# default, so a show whose venue sells direct still sits at the top of the list
# where it belongs. It is only absent from "on sale today", which is honest —
# the festival is not selling it today.
BOOKABLE = ("TICKETS_AVAILABLE", "TWO_FOR_ONE", "PREVIEW_SHOW", "EVENT_SPECIFIC",
            "FREE_TICKETED", "FREE_NON_TICKETED")


def _performances(html: str) -> list[tuple[str, str, str]]:
    """Every performance as (date, time, status), on an Edinburgh clock."""
    out = []
    for p in _json_array(html, "performances") or []:
        stamp = p.get("dateTime") if isinstance(p, dict) else None
        if not isinstance(stamp, str) or len(stamp) < 16:
            continue
        try:
            when = datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
        if stamp.endswith("Z") or stamp[19:].startswith("+00"):
            when = when.replace(tzinfo=timezone.utc).astimezone(EDINBURGH)
        if p.get("cancelled") or p.get("soldOut"):
            status = CANCELLED if p.get("cancelled") else "SOLD_OUT"
        else:
            status = p.get("ticketStatus") or ""
        out.append((when.strftime("%Y-%m-%d"), when.strftime("%H:%M"), status))
    return out


def _official_title(html: str) -> str:
    """
    What the festival itself calls the show.

    Publications name a run however suits their headline — The Skinny wrote
    "Dane Buckley @ Pleasance Courtyard", so that is the name we took and kept.
    The programme calls it "Dane Buckley: Darling", and the programme is the
    show's own name rather than one paper's shorthand for it.

    Mostly this restores a performer ("Sarah Hester Ross: Serving C*nt") or fixes
    capitals that a headline shouted ("44 MINUTES" -> "44 Minutes").
    """
    import json

    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  html, re.S)
    if not m:
        return ""
    try:
        event = json.loads(m.group(1))["props"]["pageProps"]["data"]["event"]
    except (KeyError, TypeError, ValueError):
        return ""
    title = (event.get("title") or "").strip() if isinstance(event, dict) else ""
    # A title that is only a venue or a date is not a title. Nothing seen so far
    # trips this, but adopting the programme's word for it means adopting
    # whatever it says, and a blank board is a bad way to find that out.
    return title if 2 < len(title) <= 160 else ""


def _presented_by(html: str) -> str:
    """
    The company as the programme credits it.

    Some entries write the credit as a sentence — "Avalon & Tellus Studio
    present" — so the trailing verb is trimmed. What is wanted is the name, not
    the sentence it appears in.
    """
    import json

    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  html, re.S)
    if not m:
        return ""
    try:
        event = json.loads(m.group(1))["props"]["pageProps"]["data"]["event"]
    except (KeyError, TypeError, ValueError):
        return ""
    if not isinstance(event, dict):
        return ""
    name = (event.get("presentedBy") or "").strip()
    name = re.sub(r"\s+(presents?|present|in association with)\.?$", "", name, flags=re.I)
    return name.strip(" .,") if 1 < len(name) <= 120 else ""


def store_company(conn: sqlite3.Connection, show_id: str, name: str) -> bool:
    """Record the company, adding the column the first time it is needed."""
    if not name:
        return False
    columns = {row[1] for row in conn.execute("PRAGMA table_info(shows)")}
    if "presented_by" not in columns:
        conn.execute("ALTER TABLE shows ADD COLUMN presented_by TEXT")
    before = conn.execute("SELECT presented_by FROM shows WHERE id = ?",
                          (show_id,)).fetchone()
    conn.execute("UPDATE shows SET presented_by = ? WHERE id = ?", (name, show_id))
    return not before or before[0] != name


def adopt_title(conn: sqlite3.Connection, show_id: str, current: str,
                official: str) -> bool:
    """
    Take the festival's name for a show, keeping ours as an alias.

    The old spelling has to stay searchable: it is what publications use, so
    dropping it would stop tomorrow's review from matching the show it belongs
    to. Page URLs are built from the show id, not the title, so nothing anyone
    has linked to moves.
    """
    if not official or official == current:
        return False
    conn.execute("UPDATE shows SET title = ? WHERE id = ?", (official, show_id))
    conn.execute("INSERT OR IGNORE INTO aliases (alias, show_id) VALUES (?, ?)",
                 (normalise(current), show_id))
    return True


def store_performances(conn: sqlite3.Connection, show_id: str,
                       performances: list[tuple[str, str, str]]) -> None:
    """
    Replace what we hold for one show.

    Replaced rather than merged: a performance that disappears from the
    programme has been withdrawn, and leaving it behind would keep offering a
    date that no longer exists.
    """
    conn.executescript(PERFORMANCES_SCHEMA)
    conn.execute("DELETE FROM performances WHERE show_id = ?", (show_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO performances (show_id, date, time, status) "
        "VALUES (?, ?, ?, ?)",
        [(show_id, *p) for p in performances])
    conn.execute(
        "INSERT INTO performance_checks (show_id, checked_at) VALUES (?, datetime('now')) "
        "ON CONFLICT(show_id) DO UPDATE SET checked_at = excluded.checked_at",
        (show_id,))


def _start_time(html: str) -> str:
    """
    The show's usual start time as HH:MM, on an Edinburgh clock.

    A run lists every performance separately, but they share one time — 25
    performances of Simon Munnery, all at 11:50. The most common wins, so an
    added late show or a preview at an odd hour does not become the answer.
    """
    from collections import Counter

    performances = _json_array(html, "performances") or []
    times = Counter(
        local
        for p in performances
        if isinstance(p, dict) and not p.get("cancelled")
        and isinstance(p.get("dateTime"), str) and len(p["dateTime"]) >= 16
        and (local := _local_clock(p["dateTime"]))
    )
    return times.most_common(1)[0][0] if times else ""


SOCIALS = (
    ("bluesky",   r'https?://(?:www\.)?bsky\.app/profile/([^"\s\\/?]+)'),
    ("x",         r'https?://(?:www\.)?(?:twitter\.com|x\.com)/([A-Za-z0-9_]{2,20})'),
    ("instagram", r'https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})'),
    ("facebook",  r'https?://(?:www\.)?facebook\.com/([A-Za-z0-9_.-]{2,40})'),
)


def _socials(html: str) -> dict:
    """
    The accounts a company has listed on its own programme entry.

    Their own statement of where to find them, which is the only trustworthy
    source for this: a handle guessed from a name reaches whoever happens to
    hold it, and being congratulated for someone else's show in public is not a
    mistake that can be taken back.
    """
    out = {}
    for name, pattern in SOCIALS:
        found = re.findall(pattern, html, re.I)
        # Ignore Facebook's own furniture — sharer links and the like.
        found = [f for f in found if f.lower() not in
                 ("sharer", "sharer.php", "share.php", "profile.php", "pages", "home")]
        if found:
            out[name] = found[0]
    return out


def _fringe_details(html: str) -> dict:
    """The Fringe states genre and subGenre outright in the page JSON."""
    genre = re.search(r'"genre"\s*:\s*"([^"]*)"', html)
    sub = re.search(r'"subGenre"\s*:\s*"([^"]*)"', html)
    duration = re.search(r'"duration"\s*:\s*"?(\d+)', html)
    if not genre:
        return {}
    # "venues" is an array; the first entry's title is the venue name.
    venue = re.search(r'"venues"\s*:\s*\[\s*\{\s*"title"\s*:\s*"([^"]{2,80})"', html)
    return {"genre": genre.group(1).strip(),
            "subgenre": (sub.group(1).strip() if sub else ""),
            "venue": (venue.group(1).strip() if venue else ""),
            "start_time": _start_time(html),
            "duration": (duration.group(1) if duration else ""),
            "socials": _socials(html)}


def _eif_details(html: str) -> dict:
    """
    EIF publishes neither a machine-readable genre nor a venue. Both appear only
    inside prose descriptions, where "a night of dance and song" would be read as
    Dance for a concert, and a venue named in passing may not be the one playing.
    Rather than invent either and print it as fact, we record that the show is
    EIF and leave both empty.
    """
    return {"genre": "", "subgenre": "", "venue": ""}


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
    {
        # The other half of the Fringe. Only 31% of PBH's shows appear in the
        # official edfringe programme (measured: 209 of 673, fuzzy included), so
        # without this index the free fringe is largely invisible to the gate —
        # and those are the acts least likely to have press elsewhere.
        "key": "freefringe",
        "label": "Free Fringe",
        "sitemap": "https://freefringe.org.uk/sitemap_index.xml",
        "paths": ("/shows/",),
        "details": _no_details,
    },
    {
        "key": "art",
        "label": "Art Festival",
        "sitemap": "https://edinburghartfestival.com/sitemap_index.xml",
        "paths": ("/event/",),
        "details": _no_details,
    },
]


def _sitemap_urls(sitemap: str, paths: tuple[str, ...]) -> list[str]:
    """
    Every page URL under a sitemap, following one level of sitemap index.

    The Fringe and EIF publish a flat sitemap; the Free Fringe and Art Festival
    publish an index pointing at sub-sitemaps. Reading only the top level of
    those returns sitemap URLs and no shows at all.
    """
    raw = _get(sitemap, timeout=40)
    if raw is None:
        return []
    locs = re.findall(r"<loc>(.*?)</loc>", raw)
    if not any(u.endswith(".xml") for u in locs):
        return locs

    urls: list[str] = []
    for sub in locs:
        if not sub.endswith(".xml"):
            urls.append(sub)
            continue
        time.sleep(DELAY)
        child = _get(sub, timeout=40)
        if child:
            urls += re.findall(r"<loc>(.*?)</loc>", child)
    return urls


def programme_index(festival: dict) -> dict[str, str]:
    """{normalised title: URL} for one festival's programme."""
    index: dict[str, str] = {}
    for url in _sitemap_urls(festival["sitemap"], festival["paths"]):
        if not any(p in url for p in festival["paths"]):
            continue
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        key = normalise(slug.replace("-", " "))
        # The section page itself ("/event/") slugs to the section name, which
        # would then match any review headline containing that word.
        if not key or any(p.strip("/") == slug for p in festival["paths"]):
            continue
        index.setdefault(key, url)

        # EIF disambiguates some slugs with a trailing year ("inala-2026"), which
        # no review headline will carry. Index the bare form too so the show is
        # still findable, without letting it overwrite an exact match.
        stripped = re.sub(r"\s+(19|20)\d\d$", "", key)
        if stripped != key:
            index.setdefault(stripped, url)
    return index


_KNOWN: dict[str, tuple[str, str]] | None = None


def known_shows(refresh: bool = False) -> dict[str, tuple[str, str]]:
    """
    {normalised title: (festival key, URL)} across every festival we index.

    This is the admission list: a real show appears in its own festival's
    programme, an article *about* the festival never does. Built once per run
    and cached, because it costs a handful of requests.

    Fringe first so its entry wins a collision — it carries genre data the
    others do not, and a show listed in both is a Fringe show either way.
    """
    global _KNOWN
    if _KNOWN is not None and not refresh:
        return _KNOWN
    combined: dict[str, tuple[str, str]] = {}
    for festival in FESTIVALS:
        index = programme_index(festival)
        print(f"      {festival['label']}: {len(index)} listed")
        for key, url in index.items():
            combined.setdefault(key, (festival["key"], url))
    _KNOWN = combined
    return combined


def label(festival: str, genre: str, subgenre: str) -> str:
    """
    The badge text.

    Where a festival gives us a real classification we show it, because that is
    the informative part. Where it does not, the festival name still tells a
    reader something useful — that this is the International Festival rather
    than the Fringe.
    """
    # One spelling for the badge and the genre filter, or a reader who picks
    # "Children's Shows" from the filter is shown rows badged "CHILDRENS SHOWS"
    # and cannot tell whether they are the same thing. Title-casing the machine
    # value gave that, and "Opera" for a section that is mostly musicals.
    from .genres import section as _section
    genre = _section(genre)
    # The Fringe comma-joins every subgenre it holds, so a badge could read
    # "Childrens Shows · Musical comedy, Family-friendly" — 48 characters, wider
    # than a phone, and the page scrolled sideways because of it. The first is
    # the one that says most; the rest are noise on a row that already has a
    # title, a performer and a row of stars.
    subgenre = (subgenre or "").split(",")[0].strip()
    parts = [p for p in (genre, subgenre) if p]
    if not parts:
        # With no classification, the festival's name is worth showing only when
        # it is NOT the Fringe: "EIF" tells a reader this is the International
        # Festival, whereas "FRINGE" on a site about the Edinburgh festivals
        # says nothing at all and is just one more thing on the row.
        if not festival or festival == "fringe":
            return ""
        names = {f["key"]: f["label"] for f in FESTIVALS}
        return names.get(festival, festival.upper()).upper()
    return " · ".join(parts)


def enrich(conn: sqlite3.Connection, year: int, limit: int | None = None) -> dict[str, int]:
    """
    Attach programme URL, festival and genre to this year's unlinked shows.

    Also picks up shows the admission gate linked but could not classify. The
    gate matches against the sitemap alone, which is cheap and gives a URL and a
    festival but no genre — that only exists on the show page. Selecting purely
    on a missing URL would skip exactly those shows and quietly cost every
    tier-1 admission its genre badge. Only the Fringe publishes a genre, so no
    other festival is re-fetched looking for one that is never there.
    """
    pending = conn.execute(
        """SELECT id, title, performer, edfringe_url, festival FROM shows
            WHERE year = ?
              AND (edfringe_url IS NULL OR edfringe_url = ''
                   OR (festival = 'fringe'
                       AND (genre IS NULL OR genre = '' OR start_time IS NULL)))
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
        # The programme frequently lists a show under its performer as well
        # ("Leo Hincks: Emotional Cowboy"), while publications review it by
        # title alone. Trying both costs nothing and is the difference between
        # a genre and a blank badge.
        candidates = [normalise(row["title"])]
        if row["performer"]:
            candidates.append(normalise(f"{row['performer']} {row['title']}"))

        # A fraction in a title reaches us in whichever form the publication
        # typed — "11 1/2 Angry Men", "11½ Angry Men", or the fraction-slash
        # "11 1⁄2 Angry Men" — and those three normalise three different ways,
        # none of them matching the programme, whose own URL drops the fraction
        # and reads plain "11-angry-men". So a spelling without it is tried too.
        without = re.sub(r"\s*(?:[½¼¾⅓⅔⅛]|\d\s*[/⁄]\s*\d)\s*", " ", row["title"])
        if normalise(without) not in candidates:
            candidates.append(normalise(without))

        # And every fuller spelling any publication has used for this show. The
        # Skinny headlined "Dane Buckley @ Pleasance Courtyard", so the show was
        # stored as "Dane Buckley" and matched nothing; The Wee Review had called
        # it "Dane Buckley: Darling", which is what the programme lists.
        #
        # Only aliases that CONTAIN the title are tried. A shorter one is the
        # dangerous direction - this show also answers to "darling", and some
        # other act's Darling would be a confident, wrong match.
        title_words = set(candidates[0].split())
        for (alias,) in conn.execute(
                "SELECT alias FROM aliases WHERE show_id = ?", (row["id"],)):
            if alias not in candidates and title_words <= set(alias.split()):
                candidates.append(alias)
        # Longest first, so the most specific spelling wins where several match.
        candidates.sort(key=len, reverse=True)
        hit = next(((f, i[k]) for k in candidates for f, i in indexes if k in i), None)
        key = normalise(row["title"])

        # Still nothing: the programme may list the show under a longer name
        # beginning with what we have. A publication that headlines "Dan Tiernan
        # @ Monkey Barrel" leaves us the performer and no show, while the
        # programme has "Dan Tiernan: Quartz And All".
        #
        # Accepted only when exactly one programme entry begins with it, so
        # Spencer Jones - who has two this year - is left alone rather than
        # guessed at, and only for names of two words or more, because a
        # one-word title like "Muse" prefixes half the programme.
        if hit is None and len(key.split()) >= 2:
            for fest, index in indexes:
                starts = [k for k in index if k.startswith(key + " ")]
                if len(starts) == 1:
                    hit = (fest, index[starts[0]])
                    break
        if hit is None and row["edfringe_url"]:
            # Linked by the gate under a spelling the title alone does not
            # reproduce; the URL it found is still the right page to read.
            hit = next(((f, row["edfringe_url"]) for f in FESTIVALS
                        if f["key"] == row["festival"]), None)
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
        store_performances(conn, row["id"], _performances(html))
        for network, handle in (details.get("socials") or {}).items():
            conn.execute(
                """INSERT INTO socials (show_id, network, handle) VALUES (?, ?, ?)
                   ON CONFLICT(show_id, network) DO UPDATE SET handle = excluded.handle""",
                (row["id"], network, handle),
            )
        conn.execute(
            """UPDATE shows SET edfringe_url = ?, festival = ?, genre = ?,
                                 subgenre = ?, venue = ?, start_time = ?,
                                 duration = ? WHERE id = ?""",
            (url, fest["key"], details.get("genre", ""),
             details.get("subgenre", ""), details.get("venue", ""),
             details.get("start_time", ""), details.get("duration", ""),
             row["id"]),
        )
        matched += 1
        per_festival[fest["label"]] = per_festival.get(fest["label"], 0) + 1

    conn.commit()
    if per_festival:
        print("    linked: " + ", ".join(f"{v} {k}" for k, v in per_festival.items()))
    return {"matched": matched, "missed": missed}


def refresh_performances(conn: sqlite3.Connection, year: int,
                         stale_days: int = 4, limit: int = 200) -> dict[str, int]:
    """
    Re-read the calendar for shows whose dates we have not checked lately.

    enrich() only fetches a show's page while something is missing from it, so
    once a show is fully described it is never read again. That is right for a
    genre, which does not change, and wrong for a schedule, which does: runs get
    extended, performances get cancelled, and a date we offer that no longer
    exists is worse than no date at all.

    Bounded on purpose. The whole board is ~780 pages and the festival asks for
    a second between requests, so refreshing everything nightly would add
    thirteen minutes to a run that has better things to do. A slice each night
    brings the board round in under a week, and shows never checked go first.
    """
    conn.executescript(PERFORMANCES_SCHEMA)
    due = conn.execute(
        """SELECT s.id, s.title, s.edfringe_url, c.checked_at
             FROM shows s LEFT JOIN performance_checks c ON c.show_id = s.id
            WHERE s.year = ? AND s.edfringe_url LIKE '%edfringe%'
              AND (c.checked_at IS NULL
                   OR c.checked_at < datetime('now', ?))
            ORDER BY c.checked_at IS NOT NULL, c.checked_at
            LIMIT ?""",
        (year, f"-{stale_days} days", limit),
    ).fetchall()
    if not due:
        return {"checked": 0, "dates": 0}

    checked = dates = failed = renamed = credited = 0
    for row in due:
        time.sleep(DELAY)
        html = _get(row["edfringe_url"])
        found = _performances(html) if html else []
        # A page that will not read is not a show with no performances left, so
        # nothing is stored: an empty result would wipe a real calendar. But the
        # ATTEMPT is recorded, or the show stays permanently due and every run
        # spends its budget on the same failure. One programme page is already a
        # 404 — a withdrawn show whose URL will never parse again — and without
        # this it was picked up, failed, and picked up again forever.
        if not found:
            conn.execute(
                "INSERT INTO performance_checks (show_id, checked_at) "
                "VALUES (?, datetime('now')) ON CONFLICT(show_id) "
                "DO UPDATE SET checked_at = excluded.checked_at", (row["id"],))
            failed += 1
            continue
        store_performances(conn, row["id"], found)
        if adopt_title(conn, row["id"], row["title"], _official_title(html)):
            renamed += 1
        if store_company(conn, row["id"], _presented_by(html)):
            credited += 1
        checked += 1
        dates += len(found)
    conn.commit()
    print(f"    calendars: {checked} show(s) refreshed, {dates} performances"
          + (f", {renamed} renamed" if renamed else "")
          + (f", {credited} credited" if credited else "")
          + (f", {failed} unreadable" if failed else ""))
    return {"checked": checked, "dates": dates, "failed": failed, "renamed": renamed}


def merge_by_programme(conn: sqlite3.Connection, year: int) -> int:
    """
    Fold together shows that turn out to be the same programme entry.

    Two shows in one year pointing at the same page in the festival programme
    are one show. It happens when publications name a run differently and
    neither spelling reaches the other: "Dan Tiernan" from a review headlined
    with the venue, "Quartz And All" from one headlined with the title. Both sat
    on the leaderboard, each with a fraction of the reviews.

    The title kept is decided by the programme, not by us. Its entry reads
    "marty gleeson dog ear", so the show we hold as "Marty Gleeson" is a prefix
    of it and the one we hold as "Dog Ear" is not: the prefix is the performer,
    the remainder is the show, and the show is what a reader is looking for.
    Counting reviews instead kept "Mike Wozniak" over "The Bench".
    """
    keys = {}
    for fest in FESTIVALS:
        for key, url in programme_index(fest).items():
            keys.setdefault(url, key)

    groups: dict[str, list] = {}
    for row in conn.execute(
            """SELECT id, title, edfringe_url,
                      (SELECT COUNT(*) FROM reviews WHERE show_id = shows.id) AS n
                 FROM shows
                WHERE year = ? AND edfringe_url IS NOT NULL AND edfringe_url <> ''""",
            (year,)):
        groups.setdefault(row["edfringe_url"], []).append(row)

    merged = 0
    for url, shows in groups.items():
        if len(shows) < 2:
            continue
        key = keys.get(url, "")
        def rank(r):
            mine = normalise(r["title"])
            # Not a prefix of the programme's own name -> the show's own title.
            return (bool(key) and not key.startswith(mine + " "), r["n"], len(r["title"]))
        shows.sort(key=rank, reverse=True)
        keeper, rest = shows[0], shows[1:]
        for other in rest:
            conn.execute("UPDATE reviews SET show_id = ? WHERE show_id = ?",
                         (keeper["id"], other["id"]))
            conn.execute("UPDATE OR IGNORE aliases SET show_id = ? WHERE show_id = ?",
                         (keeper["id"], other["id"]))
            conn.execute("DELETE FROM aliases WHERE show_id = ?", (other["id"],))
            conn.execute("DELETE FROM shows WHERE id = ?", (other["id"],))
            print(f"    merged {other['title'][:34]!r} into {keeper['title'][:34]!r}")
            merged += 1
    conn.commit()
    return merged
