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

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.collect import load_config                    # noqa: E402

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
    if re.search(r'src="[^"]*?(\d)(?:andahalf)?stars?\.(?:png|gif|svg|jpg)', html, re.I):
        found.append("rating in an image filename")
    if re.search(r'"(?:ratingValue|reviewRating)"', html):
        found.append("structured data")
    if re.search(r"\b(?:one|two|three|four|five|[1-5])\s+stars\b", html, re.I):
        found.append("written in words")
    if re.search(r'class="[^"]*(?:star|rating)[^"]*"', html, re.I):
        found.append("a CSS class")
    if re.search(r'width:\s*\d{1,3}%[^"]*"[^>]*class="[^"]*star', html, re.I):
        found.append("a bar width")
    return found


def sample_review(session, base, feed_url):
    """A likely review URL, from the feed if there is one, else the home page."""
    if feed_url:
        text = get(session, feed_url).text or ""
        links = re.findall(r"<link[^>]*>([^<]+)</link>|<link[^>]*href=\"([^\"]+)\"", text)
        flat = [a or b for a, b in links]
        for u in flat:
            if "review" in u.lower():
                return u
        if len(flat) > 1:
            return flat[1]
    html = get(session, base + "/").text or ""
    hrefs = re.findall(r'href="([^"]+)"', html)
    for h in hrefs:
        if "review" in h.lower() and "#" not in h:
            return urljoin(base, h)
    return None


def probe(session, host: str) -> str:
    base = host if host.startswith("http") else "https://" + host
    base = f"{urlsplit(base).scheme}://{urlsplit(base).netloc}"
    verdict, allowed = robots(session, base)
    if not allowed:
        return f"{verdict}"
    feed = find_feed(session, base)
    url = sample_review(session, base, feed)
    if not url:
        return f"{verdict}; no feed, and no review link found to sample"
    page = get(session, url)
    if page.status_code != 200:
        return f"{verdict}; feed={'yes' if feed else 'no'}; sample page HTTP {page.status_code}"
    styles = rating_style(page.text)
    where = "feed" if feed else "no feed (index or sitemap needed)"
    if not styles:
        return f"{verdict}; {where}; NO RATING FOUND on {url[-40:]}"
    return f"{verdict}; {where}; rating via {', '.join(styles)}"


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
