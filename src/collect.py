"""
Stage 1: find candidate review URLs, and stage 2: pull a rating out of each.

These are one module because they share the HTTP session and the politeness
delay. Nothing here needs an API key.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import feedparser
import requests
import yaml

from . import db
from .ratings import Rating, find_rating

CONFIG_PATH = Path(__file__).resolve().parent.parent / "sources.yaml"


@dataclass
class Candidate:
    url: str
    title: str
    summary: str
    published: str | None
    publication: str


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

    # -- HTTP ---------------------------------------------------------------

    def _get(self, url: str) -> str | None:
        """Fetch a URL, never raising. Rate-limited to one request per `delay`."""
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
        try:
            r = self.session.get(url, timeout=self.timeout)
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
        if backfill and pub.get("backfill"):
            year, month = backfill
            base = pub["backfill"].format(year=year, month=month)

        pages = int(pub.get("pages", 1))
        if pages <= 1:
            return [base]

        joiner = "&" if "?" in base else "?"
        return [base if p == 1 else f"{base}{joiner}paged={p}" for p in range(1, pages + 1)]

    def discover(self, pub: dict, backfill: tuple[int, int] | None) -> list[Candidate]:
        found: list[Candidate] = []
        for feed_url in self.feed_urls(pub, backfill):
            raw = self._get(feed_url)
            if raw is None:
                continue
            parsed = feedparser.parse(raw)
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
    )

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
    def looks_like_review(cls, cand: Candidate) -> bool:
        title = cand.title.lower()
        return not any(phrase in title for phrase in cls.NOT_A_REVIEW)

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
        return any(marker in title for marker in cls.EDINBURGH_MARKERS)

    # -- Stage 2: rating extraction ----------------------------------------

    def rate(self, pub: dict, cand: Candidate) -> Rating | None:
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

    for pub in collector.publications:
        print(f"  {pub['name']}")
        candidates = collector.discover(pub, backfill)
        if limit:
            candidates = candidates[:limit]
        print(f"    {len(candidates)} items in feed")

        new = [c for c in candidates if not db.already_seen(conn, c.url)]
        print(f"    {len(new)} not seen before")

        before = len(new)
        new = [c for c in new if collector.looks_like_review(c)]
        if before != len(new):
            print(f"    {before - len(new)} dropped as news/interviews")

        if pub.get("require_edinburgh"):
            before = len(new)
            new = [c for c in new if collector.is_edinburgh(c)]
            print(f"    {len(new)} look like Edinburgh shows (of {before})")

        rated = 0
        for cand in new:
            rating = collector.rate(pub, cand)
            if rating is None:
                db.mark_seen(conn, cand.url, "no_rating")
                continue

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
            db.mark_seen(conn, cand.url, "review")
            rated += 1

        conn.commit()
        print(f"    {rated} with a star rating")
        added += rated

    return added
