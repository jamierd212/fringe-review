"""
Assess whether a publication could be collected, without guessing.

Adding a source is cheap; finding out whether it is worth adding is the slow
part, and it has been done by hand each time. This asks the three questions that
decide it, in order, and stops as soon as the answer is no:

    may we read it   robots.txt, read the way the collector reads it
    can we find it   a feed, or a sitemap, or neither
    is there a rating  and in what form - characters, an image, structured data,
                       a CSS class, or nothing at all

The last is the one that catches people out. BroadwayWorld looked unrated
through three separate checks because their rating is the filename of an image,
and EdFringeReview looked empty because their pages are rendered in the browser.
Both are tested for here.

    python tools/probe_sources.py                 # the built-in candidate list
    python tools/probe_sources.py example.com ... # specific hosts
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import time

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.collect import load_config                    # noqa: E402
from src.ratings import find_rating                   # noqa: E402

UA = load_config()["defaults"].get(
    "user_agent", "FringeLeaderboardBot/0.1 (+https://fringestars.com/bot.html)")
TOKEN = UA.split("/")[0]

FEED_PATHS = ("/feed/", "/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml",
              "/index.xml", "/?feed=rss2")
STAR_CHARS = "★⭐✭✦☆✩"


def get(session, url, **kw):
    try:
        return session.get(url, timeout=20, allow_redirects=True, **kw)
    except requests.RequestException as exc:
        return type("Failed", (), {"status_code": None, "text": "",
                                   "headers": {}, "url": url,
                                   "error": type(exc).__name__})()


def robots(session, base):
    """(verdict, may_we_crawl). Mirrors the collector's reading of the rules."""
    r = get(session, base + "/robots.txt")
    if r.status_code is None:
        return f"unreachable ({getattr(r, 'error', '?')})", False
    if r.status_code in (401, 403):
        return f"refuses robots.txt ({r.status_code})", False
    if r.status_code != 200:
        return f"no robots.txt ({r.status_code}) — nothing forbidden", True
    parser = RobotFileParser()
    parser.parse(r.text.splitlines())
    allowed = parser.can_fetch(TOKEN, base + "/some-review/")
    delay = parser.crawl_delay(TOKEN)
    note = "allows us" if allowed else "DISALLOWS us"
    if delay:
        note += f", asks {delay}s between requests"
    return note, allowed


def find_feed(session, base):
    html = get(session, base + "/").text or ""
    declared = re.findall(
        r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]*href="([^"]+)"', html, re.I)
    for href in declared[:3]:
        url = urljoin(base, href)
        r = get(session, url)
        if r.status_code == 200 and r.text.lstrip()[:5] in ("<?xml", "<rss ", "<feed"):
            return url
    for path in FEED_PATHS:
        r = get(session, base + path)
        if r.status_code == 200 and r.text.lstrip()[:5] in ("<?xml", "<rss ", "<feed"):
            return r.url
    return None


def rating_style(html: str) -> list[str]:
    """Every way this page might be stating a star rating."""
    found = []
    if any(c in html for c in STAR_CHARS):
        found.append("star characters")
    # Loose on purpose. BroadwayWorld writes 4stars.png and Theatre Weekly
    # New-4Star-350x71.png; an exact shape catches one and misses the other, and
    # a missed rating reads as "this publication doesn't rate", which is the
    # wrong answer to give about a publication that plainly does.
    if re.search(r'src="[^"]*?(\d)\s*(?:andahalf)?[-_ ]?stars?\b[^"]*"', html, re.I):
        found.append("rating in an image filename")
    if re.search(r'alt="[^"]*\b(?:one|two|three|four|five|\d)[-\s]?stars?\b[^"]*"', html, re.I):
        found.append("rating in an image's alt text")
    if re.search(r'"(?:ratingValue|reviewRating)"', html):
        found.append("structured data")
    if re.search(r"\b(?:one|two|three|four|five|[1-5])\s+stars\b", html, re.I):
        found.append("written in words")
    if re.search(r'class="[^"]*(?:star|rating)[^"]*"', html, re.I):
        found.append("a CSS class")
    if re.search(r'width:\s*\d{1,3}%[^"]*"[^>]*class="[^"]*star', html, re.I):
        found.append("a bar width")
    return found


def sample_reviews(session, base, feed_url, want=2):
    """
    Real article URLs, never the feed itself.

    British Theatre Guide was written off because their atom feed lives at
    /reviews.atom, so "the first link containing 'review'" was the feed, and
    reading a feed as though it were an article finds no rating in it.
    """
    out = []
    if feed_url:
        import feedparser
        parsed = feedparser.parse(get(session, feed_url).text or "")
        for entry in parsed.entries:
            link = entry.get("link")
            if link and link.rstrip("/") != feed_url.rstrip("/"):
                out.append(link)
            if len(out) >= want:
                return out
    html = get(session, base + "/").text or ""
    for href in re.findall(r'href="([^"]+)"', html):
        url = urljoin(base, href)
        if url.rstrip("/") in (base, feed_url or ""):
            continue
        if re.search(r"/(?:reviews?|edinburgh)/", url) and "#" not in url:
            if url not in out:
                out.append(url)
        if len(out) >= want:
            break
    return out


def probe(session, host: str) -> str:
    base = host if host.startswith("http") else "https://" + host
    base = f"{urlsplit(base).scheme}://{urlsplit(base).netloc}"
    verdict, allowed = robots(session, base)
    if not allowed:
        return f"{verdict}"
    feed = find_feed(session, base)
    urls = sample_reviews(session, base, feed)
    if not urls:
        return f"{verdict}; no feed, and no article found to sample"
    where = "feed" if feed else "no feed (index needed)"
    lines = []
    for url in urls:
        time.sleep(2)                      # two pages per site, unhurried
        page = get(session, url)
        if page.status_code != 200:
            lines.append(f"HTTP {page.status_code}")
            continue
        found = find_rating(html=page.text, rule={"type": "auto"})
        styles = rating_style(page.text)
        got = f"{found.stars}* via {found.method}" if found else "extractor found nothing"
        lines.append(f"{got}" + (f" [{'; '.join(styles)}]" if styles else " [no signal]"))
    return f"{verdict}; {where}; " + " | ".join(lines)


CANDIDATES = [
    "thestage.co.uk", "timeout.com", "beyondthejoke.co.uk",
    "everything-theatre.co.uk", "theatreweekly.com", "theupcoming.co.uk",
    "britishtheatreguide.info", "allthatdazzles.co.uk", "acrossthearts.co.uk",
    "edinburghguide.com", "exeuntmagazine.com", "theartsdesk.com",
    "londontheatre1.com", "ayoungertheatre.com", "theatrefullstop.com",
    "viewsfromthegods.co.uk", "bunburymagazine.com", "thenewcurrent.co.uk",
    "thespyinthestalls.com", "rgm.press", "fringefan.com",
    "edinburgh-reviews.co.uk", "heraldscotland.com", "thenational.scot",
    "sundaypost.com", "dailyrecord.co.uk", "inews.co.uk", "standard.co.uk",
    "metro.co.uk", "independent.co.uk", "scotsgay.co.uk", "themumble.co.uk",
]


def main() -> int:
    session = requests.Session()
    session.headers["User-Agent"] = UA
    hosts = sys.argv[1:] or CANDIDATES
    have = {p["name"].casefold() for p in load_config()["publications"]}
    print(f"  probing {len(hosts)} hosts as {TOKEN}\n")
    for host in hosts:
        print(f"  {host:26} {probe(session, host)}")
    print(f"\n  ({len(have)} publications already configured)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
