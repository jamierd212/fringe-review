"""
Stage 1: find candidate review URLs, and stage 2: pull a rating out of each.

These are one module because they share the HTTP session and the politeness
delay. Nothing here needs an API key.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import requests
import yaml

from . import db
from .normalise import normalise
from .ratings import Rating, find_rating, split_roundup

CONFIG_PATH = Path(__file__).resolve().parent.parent / "sources.yaml"


def _fragment(title: str) -> str:
    """A URL-safe, stable key for one show within a round-up article."""
    return re.sub(r"[^a-z0-9]+", "-", normalise(title)).strip("-")[:60] or "show"


@dataclass
class Candidate:
    url: str
    title: str
    summary: str
    published: str | None
    publication: str
    # Set when the source hands us the rating outright (the Guardian's API has a
    # starRating field). Saves a page fetch and removes any guesswork.
    known_stars: int | None = None


class Collector:
    def __init__(self, config: dict):
        self.defaults = config.get("defaults", {})
        self.publications = [
            p for p in config["publications"] if p.get("enabled", True)
        ]
        self.session = requests.Session()
        self.session.headers["User-Agent"] = self.defaults.get(
            "user_agent", "FringeLeaderboardBot/0.1"
        )
        self.delay = float(self.defaults.get("delay_seconds", 1.0))
        self.timeout = float(self.defaults.get("timeout_seconds", 25))
        self._last_request = 0.0
        self.last_status = 0

    # -- HTTP ---------------------------------------------------------------

    def _get(self, url: str) -> str | None:
        """Fetch a URL, never raising. Rate-limited to one request per `delay`."""
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
        try:
            r = self.session.get(url, timeout=self.timeout)
            self.last_status = r.status_code
            if r.status_code != 200:
                print(f"      ! HTTP {r.status_code}  {url}")
                return None
            return r.text
        except requests.RequestException as exc:
            print(f"      ! {type(exc).__name__}  {url}")
            return None

    # -- Stage 1: discovery -------------------------------------------------

    def feed_urls(self, pub: dict, backfill: tuple[int, int] | None) -> list[str]:
        """
        The live feed, or a date-archive feed when backfilling.

        WordPress serves 10 items per feed by default and accepts ?paged=N for
        older ones, so `pages:` in sources.yaml controls how far back we walk.
        During the festival 1-2 pages is plenty for a daily run; backfilling a
        whole month needs more.
        """
        base = pub["feed"]
        if backfill:
            if not pub.get("backfill"):
                # No date archive. Walking the live feed 40 times returns the
                # same current items on every request - 40 pointless fetches at
                # a site that never asked to be crawled that hard, for nothing.
                return []
            year, month = backfill
            base = pub["backfill"].format(year=year, month=month)

        # A daily run only needs the newest page or two. A backfill wants
        # everything, so walk deep and let discover() stop at the first empty
        # page rather than guessing how many there are.
        pages = int(pub.get("backfill_pages", 40)) if backfill else int(pub.get("pages", 1))
        if pages <= 1:
            return [base]

        joiner = "&" if "?" in base else "?"
        return [base if p == 1 else f"{base}{joiner}paged={p}" for p in range(1, pages + 1)]

    def discover_guardian(self, pub: dict,
                          backfill: tuple[int, int] | None) -> list[Candidate]:
        """
        The Guardian's Open Platform API instead of its RSS feed.

        Two reasons this is the right route for a national. Their star ratings
        render as SVG components, so nothing in the HTML is scrapable — but the
        API returns `starRating` as an integer field. And their RSS feed is a
        rolling four-month window with no archive, so a backfill is impossible
        from it; the API takes an explicit date range.

        `test` is the Guardian's own public trial key and works without
        registration, but it is rate-limited. Set GUARDIAN_API_KEY to a free
        developer key (12,000 calls/day) before relying on this daily.
        """
        import calendar
        import json
        import os
        import urllib.parse

        key = os.environ.get("GUARDIAN_API_KEY", "test")
        if backfill:
            year, month = backfill
            last = calendar.monthrange(year, month)[1]
            date_range = {"from-date": f"{year}-{month:02d}-01",
                          "to-date": f"{year}-{month:02d}-{last}"}
        else:
            # An unbounded search walks the whole tag: 20 pages of 50 reaches
            # back to 2019, and every August in it passes a window filter that
            # only compares month and day. Daily runs see this year only.
            today = datetime.now()
            date_range = {"from-date": f"{today.year}-01-01",
                          "to-date": today.strftime("%Y-%m-%d")}

        found: list[Candidate] = []
        page = 1
        while page <= int(pub.get("api_pages", 20)):
            params = {
                "api-key": key,
                "tag": pub.get("tag", "culture/edinburghfestival"),
                "show-fields": "starRating,headline",
                "page-size": 50,
                "page": page,
                **date_range,
            }
            raw = self._get("https://content.guardianapis.com/search?"
                            + urllib.parse.urlencode(params))
            if raw is None:
                break
            try:
                resp = json.loads(raw)["response"]
            except (ValueError, KeyError):
                break

            for item in resp.get("results", []):
                fields = item.get("fields", {})
                stars = fields.get("starRating")
                if stars is None:
                    continue          # a feature or interview, not a review
                found.append(Candidate(
                    url=item["webUrl"],
                    title=fields.get("headline") or item.get("webTitle", ""),
                    summary="",
                    published=item.get("webPublicationDate"),
                    publication=pub["name"],
                    known_stars=int(stars),
                ))

            if page >= resp.get("pages", 1):
                break
            page += 1
        return found

    def find_index_urls(self, pub: dict,
                        backfill: tuple[int, int] | None) -> list[str]:
        """
        Locate this year's index page instead of hard-coding it.

        Chortle's index lives at a URL carrying an unguessable numeric id
        (.../58413/edinburgh_fringe_2025_comedy_reviews), so a config entry has
        to be added by hand every year — and if it is forgotten the source
        silently contributes nothing, with no error to notice.

        The *slug* is predictable though. So we scan a couple of hub pages for
        any link containing `edinburgh_fringe_<year>_comedy_reviews` and use
        whatever we find. The moment Chortle publishes and links this year's
        index, it starts working on its own.
        """
        import datetime as _dt
        import re as _re

        discovery = pub.get("index_discovery")
        if not discovery:
            return []

        year = backfill[0] if backfill else _dt.date.today().year
        slug = str(discovery.get("slug", "")).format(year=year)
        if not slug:
            return []

        found: list[str] = []
        for hub in discovery.get("hubs", []):
            raw = self._get(hub.format(year=year))
            if raw is None:
                continue
            for href in _re.findall(r'href="([^"]+)"', raw):
                if slug in href:
                    url = href if href.startswith("http") else \
                        "https://www.chortle.co.uk" + href
                    found.append(url)
        if found:
            print(f"    found {len(set(found))} index page(s) for {year}")
        return found

    def discover_index(self, pub: dict,
                       backfill: tuple[int, int] | None) -> list[Candidate]:
        """
        Publications with no usable feed, discovered by walking an index page.

        Chortle publishes every festival review on one page but offers only a
        three-item news RSS, so the index is the only route in — and it is a
        good one: link, title and date all come from a single request, and the
        page is updated as new reviews are published, so re-fetching it daily
        picks up whatever is new.

        The index URL contains an unpredictable numeric id, so it cannot be
        derived and has to be configured per festival year.
        """
        import re as _re

        pattern = pub.get("link_pattern")
        if not pattern:
            return []

        index_urls = list(pub.get("index_urls", []))
        index_urls += self.find_index_urls(pub, backfill)

        found: list[Candidate] = []
        for index_url in dict.fromkeys(index_urls):
            raw = self._get(index_url)
            if raw is None:
                continue
            for url, label in dict.fromkeys(_re.findall(pattern, raw, _re.S)):
                # The date sits in the URL, which is the only date this source
                # gives us — without it every review would fall back to the
                # current year and land on the wrong leaderboard.
                m = _re.search(r"/(\d{4})/(\d{2})/(\d{2})/", url)
                published = f"{m.group(1)}-{m.group(2)}-{m.group(3)}T12:00:00Z" if m else None
                if backfill and m:
                    if (int(m.group(1)), int(m.group(2))) != backfill:
                        continue
                title = _re.sub(r"\s+", " ", _re.sub(r"<[^>]+>", "", label)).strip()
                found.append(Candidate(url=url, title=title, summary="",
                                       published=published, publication=pub["name"]))
        return found

    def expand_roundup(self, pub: dict,
                       cand: Candidate) -> list[tuple[Candidate, Rating]]:
        """
        Turn one multi-show round-up into one candidate per show.

        Each piece keeps the article's URL with a fragment appended, because
        reviews are keyed by URL and five shows from one article need five keys.
        The fragment is also honest: the link still opens the article the rating
        was published in.
        """
        html = self._get(cand.url)
        if html is None:
            return []
        out = []
        for title, rating in split_roundup(html):
            out.append((Candidate(
                url=f"{cand.url}#{_fragment(title)}", title=title, summary="",
                published=cand.published, publication=pub["name"],
            ), rating))
        return out

    def discover_sitemap(self, pub: dict, backfill: tuple[int, int] | None) -> list[Candidate]:
        """
        Discovery from a publisher's own month-by-month article sitemap.

        Some publications have no usable feed but do publish dated sitemaps.
        The Scotsman is the case in point: its RSS is a rolling nine items with
        no archive, while sitemap-articles-2025-08.xml lists 2,677 articles for
        that month alone, 175 of them festival reviews. So the sitemap is not a
        fallback here, it is the better source - it makes a real backfill
        possible and gives a daily run the whole month rather than whatever
        happens to be on the front of the feed.

        Only URLs matching `url_must_match` are returned, because a month
        sitemap is the entire newspaper. Titles come from the slug; the real
        headline is read from the page when it is fetched for its rating.
        """
        import calendar as _cal

        when = backfill or (datetime.now().year, datetime.now().month)
        year, month = when
        template = pub["sitemap"]
        raw = self._get(template.format(year=year, month=month))
        if raw is None:
            return []

        patterns = [re.compile(p, re.I) for p in pub.get("url_must_match", [])]
        found: list[Candidate] = []
        for block in re.findall(r"<url>(.*?)</url>", raw, re.S):
            loc = re.search(r"<loc>(.*?)</loc>", block)
            if not loc:
                continue
            url = loc.group(1)
            if patterns and not all(p.search(url) for p in patterns):
                continue

            # Take <lastmod> as the publication date. Without a date the year
            # would fall back to whatever the run was invoked with, which is
            # precisely how a --match run once relabelled every 2025 show as
            # 2026, and the festival-window filter would pass everything.
            mod = re.search(r"<lastmod>(.*?)</lastmod>", block)
            slug = re.sub(r"-\d{5,}$", "", url.rstrip("/").rsplit("/", 1)[-1])
            found.append(Candidate(
                url=url, title=slug.replace("-", " ").strip(),
                summary="", published=mod.group(1) if mod else None,
                publication=pub["name"],
            ))
        _ = _cal
        return found

    def discover(self, pub: dict, backfill: tuple[int, int] | None) -> list[Candidate]:
        if pub.get("api") == "guardian":
            return self.discover_guardian(pub, backfill)
        if pub.get("discovery") == "index":
            return self.discover_index(pub, backfill)
        if pub.get("discovery") == "sitemap":
            return self.discover_sitemap(pub, backfill)

        found: list[Candidate] = []
        for feed_url in self.feed_urls(pub, backfill):
            raw = self._get(feed_url)
            if raw is None:
                # A 404 while walking ?paged=N means we ran past the last page.
                # Fest returns a whole month on page 1 then 404s, so without this
                # every backfill burns 39 pointless requests per source. Other
                # failures may be transient, so only 404 ends the walk.
                if self.last_status == 404:
                    break
                continue
            parsed = feedparser.parse(raw)
            if not parsed.entries:
                # WordPress returns an empty channel past the last page, which is
                # the only reliable signal that an archive is exhausted.
                break
            for entry in parsed.entries:
                link = (entry.get("link") or "").strip()
                if not link:
                    continue
                # feedparser exposes content:encoded as entry.content
                content = ""
                if entry.get("content"):
                    content = " ".join(c.get("value", "") for c in entry.content)
                found.append(
                    Candidate(
                        url=link,
                        title=entry.get("title", ""),
                        summary=f"{entry.get('summary', '')} {content}",
                        published=entry.get("published", None),
                        publication=pub["name"],
                    )
                )
        return found

    # -- Filtering ---------------------------------------------------------

    # Other festivals that run in August and also call themselves a "Fringe".
    # Checked first, because "Camden Fringe 2025: Spin Cycle - Etcetera Theatre,
    # London" contains the word "fringe" and would otherwise sail through.
    OTHER_FESTIVALS = (
        "camden fringe", "brighton fringe", "buxton fringe", "greater manchester fringe",
        "prague fringe", "adelaide fringe", "vault festival", "off-west end",
        # Scottish, festival-season, and not what this leaderboard covers. The
        # Scotsman reviews all of them alongside the Edinburgh festivals.
        "fringe by the sea", "pittenweem", "celtic connections", "hebridean",
    )

    # Markers too short to match as substrings: "eif" would fire on "Eiffel".
    # Checked with word boundaries instead.
    EDINBURGH_MARKER_WORDS = (r"\beif\b",)

    EDINBURGH_MARKERS = (
        "edinburgh", "edfringe", "ed fringe", "summerhall", "pleasance",
        "gilded balloon", "underbelly", "assembly roxy", "assembly george",
        "traverse", "zoo southside", "just the tonic", "monkey barrel",
    )

    # Headline phrases that mean "this is news, not a review". Publications post
    # award announcements and programme launches into the same feed, and those
    # pages contain stray numbers that the rating cascade will happily misread.
    # Kept deliberately tight: a dropped review costs us one data point, but a
    # news item scored five stars puts a fictional show at the top of the page.
    NOT_A_REVIEW = (
        "programme launch", "announces", "announced", "line-up", "lineup",
        "tickets on sale", "nominations", "award winners", "winners announced",
        "in conversation", "interview:", "preview:", "q&a", "obituary",
        "applications open", "call for", "round-up", "roundup", "what's on",
    )

    @classmethod
    def looks_like_review(cls, cand: Candidate, pub: dict | None = None) -> bool:
        """
        A publication that titles every review the same way gives us something far
        better than a blocklist: `require_title_prefix` keeps only those items.

        This matters more than it looks. Fest's end-of-festival round-up carries
        39 star glyphs from the shows it lists, so the rating cascade happily
        scores it 5 stars — and a phantom show called "The Very Best of the
        Edinburgh Festivals 2025" lands at the top of the leaderboard. A
        blocklist can only chase those one phrase at a time; an allowlist of
        title shapes cannot be surprised.
        """
        title = cand.title.strip().lower()

        # Per-publication exclusions, for sources with no consistent review
        # prefix to allowlist. Deliberately scoped to one publication each:
        # global keyword rules were measured against the 2025 data and flagged
        # "The 39 Steps" and "A Broken Man's Guide to Fixing Others" as junk,
        # so a pattern is only ever applied where it has been checked.
        if pub:
            for pattern in pub.get("exclude_title_patterns", []) or []:
                if re.search(pattern, cand.title, re.I):
                    return False
        if pub and (prefix := pub.get("require_title_prefix")):
            if not title.startswith(prefix.strip().lower()):
                return False
        return not any(phrase in title for phrase in cls.NOT_A_REVIEW)

    @staticmethod
    def _published_at(cand: Candidate) -> datetime | None:
        """
        A candidate's publication date, or None if it has none we can read.

        RSS uses RFC 2822 ("Wed, 27 Aug 2025 10:34:44 +0000"), JSON APIs use ISO
        8601 ("2025-08-27T10:34:44Z"). Both callers need both formats, and when
        they disagreed about which to parse the Guardian's dates silently fell
        through to a fallback year.
        """
        if not cand.published:
            return None
        try:
            return parsedate_to_datetime(cand.published)
        except (TypeError, ValueError):
            pass
        try:
            return datetime.fromisoformat(cand.published.replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError):
            return None

    def in_target_year(self, cand: Candidate, target: int) -> bool:
        """
        Does this review belong to the festival we are actually collecting?

        The window filter only compares month and day, so 12 August 2019 sits
        inside it exactly as 12 August 2026 does. That is deliberate — the window
        answers "is this festival season?" — but on its own it lets a source with
        a deep archive backfill itself. A daily sweep once walked the Guardian's
        entire Edinburgh tag and published leaderboards back to 2019.

        A review with no readable date is kept, matching in_festival_window: a
        missing date is a feed quirk, not evidence of the wrong year.
        """
        published = self._published_at(cand)
        return published is None or published.year == target

    def in_festival_window(self, cand: Candidate, pub: dict | None = None) -> bool:
        """
        Was this published during a festival, rather than the rest of the year?

        Skipped entirely for publications whose feed is already Fringe-only
        (`festival_feed: true`). FringeReview posts Edinburgh reviews for weeks
        after the festival ends — one arrived in October — and those are still
        Fringe reviews. Applying a date window to a feed that is Fringe-by-
        definition just throws away late coverage.

        Reviews with no parseable date are kept — a missing date is a feed quirk,
        not evidence the show isn't a Fringe show, and dropping them would lose
        real reviews silently.
        """
        if pub and pub.get("festival_feed"):
            return True
        window = self.defaults.get("festival_window")
        published = self._published_at(cand)
        if not window or published is None:
            return True

        start_m, start_d = (int(x) for x in str(window["start"]).split("-"))
        end_m, end_d = (int(x) for x in str(window["end"]).split("-"))
        return (start_m, start_d) <= (published.month, published.day) <= (end_m, end_d)

    @classmethod
    def is_edinburgh(cls, cand: Candidate) -> bool:
        """
        Publications like The Reviews Hub cover the whole UK, so during August
        their feed mixes Edinburgh with Camden Fringe and regular London runs.

        The headline is the reliable signal — it carries the city or the festival
        name. We deliberately do not search the article body: a London review that
        mentions "returning after a sell-out Edinburgh run" would match, and a
        false include puts a wrong show on the leaderboard.
        """
        title = cand.title.lower()
        if any(other in title for other in cls.OTHER_FESTIVALS):
            return False
        if any(marker in title for marker in cls.EDINBURGH_MARKERS):
            return True
        return any(re.search(w, title) for w in cls.EDINBURGH_MARKER_WORDS)

    # -- Stage 2: rating extraction ----------------------------------------

    def rate(self, pub: dict, cand: Candidate) -> Rating | None:
        # An API that states the rating outright beats anything we could infer.
        if cand.known_stars is not None:
            return Rating(stars=cand.known_stars, original=f"{cand.known_stars}/5",
                          converted=False, rounded=False, method="api")

        rule = pub.get("rating", {})

        # Try the feed contents first — it costs nothing and describes only
        # this review, unlike a page full of sidebars.
        if rule.get("type") != "css":
            found = find_rating(title=cand.title, summary=cand.summary, rule=rule)
            if found:
                return found

        if not pub.get("fetch_page"):
            return None

        html = self._get(cand.url)
        if html is None:
            return None

        # Sitemap discovery only has the slug to go on, and a slug makes a poor
        # headline to match shows against ("chappell-roan-edinburgh-review-
        # joyous"). The page is already in hand, so take the real headline from
        # it rather than fetching twice or matching on punctuation-stripped mush.
        if pub.get("title_from_page"):
            for pattern in (r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"',
                            r"<title>(.*?)</title>"):
                m = re.search(pattern, html, re.S | re.I)
                if m:
                    import html as _html
                    cand.title = _html.unescape(m.group(1)).strip()
                    break

        return find_rating(
            title=cand.title, summary=cand.summary, html=html, rule=rule
        )


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def run(conn, backfill: tuple[int, int] | None = None, limit: int | None = None) -> int:
    """Collect and rate everything new. Returns the number of rated reviews added."""
    collector = Collector(load_config())
    added = 0
    # A backfill is explicitly asked for one festival; anything else collects
    # the current one. Nothing else may enter the database.
    target_year = backfill[0] if backfill else datetime.now().year

    for pub in collector.publications:
        print(f"  {pub['name']}")
        candidates = collector.discover(pub, backfill)
        if limit:
            candidates = candidates[:limit]
        print(f"    {len(candidates)} items in feed")

        if not candidates and pub.get("discovery") == "index":
            # An index-based source finding nothing means the index URL is stale
            # or this year's has not been configured — not that there is no news.
            # Without this the source contributes zero, silently, for a year.
            print(f"    !! NO CANDIDATES — check index_urls for {pub['name']}; "
                  f"a new one is needed each festival")

        new = [c for c in candidates if not db.already_seen(conn, c.url)]
        print(f"    {len(new)} not seen before")

        before = len(new)
        new = [c for c in new if collector.looks_like_review(c, pub)]
        if before != len(new):
            print(f"    {before - len(new)} dropped as news/interviews")

        before = len(new)
        new = [c for c in new if collector.in_festival_window(c, pub)]
        if before != len(new):
            print(f"    {before - len(new)} dropped as outside the festival window")

        before = len(new)
        new = [c for c in new if collector.in_target_year(c, target_year)]
        if before != len(new):
            print(f"    {before - len(new)} dropped as another festival year "
                  f"(collecting {target_year})")

        if pub.get("require_edinburgh"):
            before = len(new)
            new = [c for c in new if collector.is_edinburgh(c)]
            print(f"    {len(new)} look like Edinburgh shows (of {before})")

        def store(cand: Candidate, rating: Rating) -> None:
            conn.execute(
                """INSERT INTO reviews
                     (url, publication, headline, stars, original,
                      converted, rounded, published, method)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(url) DO NOTHING""",
                (
                    cand.url, cand.publication, cand.title, rating.stars,
                    rating.original, int(rating.converted), int(rating.rounded),
                    cand.published, rating.method,
                ),
            )

        rated = split_out = 0
        roundup_pattern = pub.get("roundup_pattern", r"\breviews\b")
        for cand in new:
            # A round-up reviews several shows in one article, each with its own
            # rating. Rated as one review it would pin a single score to a
            # headline naming five shows; split, it is five real reviews.
            if pub.get("roundups") and re.search(roundup_pattern, cand.title, re.I):
                pieces = collector.expand_roundup(pub, cand)
                if pieces:
                    for piece, rating in pieces:
                        store(piece, rating)
                    db.mark_seen(conn, cand.url, "roundup")
                    rated += len(pieces)
                    split_out += 1
                    continue
                # Nothing extractable: fall through and treat it as one review
                # rather than silently dropping a page that may carry a rating.

            rating = collector.rate(pub, cand)
            if rating is None:
                db.mark_seen(conn, cand.url, "no_rating")
                continue
            store(cand, rating)
            db.mark_seen(conn, cand.url, "review")
            rated += 1

        conn.commit()
        print(f"    {rated} with a star rating"
              + (f" ({split_out} round-ups split out)" if split_out else ""))
        added += rated

    return added
