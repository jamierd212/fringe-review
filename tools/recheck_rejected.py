"""
Look again at publications previously written off.

Five in a row turned out to be rateable after I had said they were not:
BroadwayWorld (rating in an image filename), Theatre Weekly (same, a shape my
detector did not match), British Theatre Guide (the probe read their feed as an
article), Time Out (five identical SVGs and the number only in a screen-reader
label) and The Arts Desk (a dead RSS feed in front of a live site).

Every one of those was a failure of my looking, not of their publishing. So this
asks the questions in the order that matters, and reports what it finds rather
than a verdict:

    is there anything current here at all
    is any of it about Edinburgh
    and on one of those pages, what does a rating look like

It tries several ways in - the feed, the home page, a reviews index, an Edinburgh
tag - because a dead feed in front of a live site is now a known failure.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ratings import find_rating                   # noqa: E402

UA = "FringeLeaderboardBot/0.1 (+https://fringestars.com/bot.html)"
EDINBURGH = re.compile(r"edinburgh|fringe|edfringe", re.I)


def get(session, url):
    try:
        return session.get(url, timeout=20, allow_redirects=True)
    except requests.RequestException:
        return None


def entry_points(base):
    return [base, f"{base}/reviews", f"{base}/reviews/", f"{base}/edinburgh",
            f"{base}/edinburgh-fringe", f"{base}/tag/edinburgh-fringe",
            f"{base}/category/reviews", f"{base}/feed/", f"{base}/rss"]


def edinburgh_links(session, base):
    """Article URLs that look like Edinburgh coverage, from any way in."""
    found: list[str] = []
    for entry in entry_points(base):
        page = get(session, entry)
        if page is None or page.status_code != 200:
            continue
        for href in re.findall(r'href="([^"#]+)"', page.text):
            url = urljoin(entry, href)
            if not url.startswith(base):
                continue
            # Assets and section indexes are not articles. Without this the
            # first "Edinburgh page" found was a web font, and a listing page
            # full of other reviews was read as one review scoring five stars.
            if re.search(r"\.(?:css|js|woff2?|png|jpe?g|svg|gif|ico|xml)(?:\?|$)", url, re.I):
                continue
            if url.rstrip("/").count("/") < 4:
                continue
            if EDINBURGH.search(url) and re.search(r"/[a-z0-9-]{20,}", url):
                if url not in found:
                    found.append(url)
        if len(found) >= 3:
            break
    return found[:3]


def signals(html):
    out = []
    for label, pat in (
        ("star chars",  r"[★⭐✭✦]{1,5}"),
        ("img src",     r'<img[^>]+src="[^"]*\b\d?[-_ ]?stars?\b[^"]*"'),
        ("img alt",     r'<img[^>]+alt="[^"]*\bstars?\b[^"]*"'),
        ("jsonld",      r'"ratingValue"'),
        ("microdata",   r'itemprop="ratingValue"'),
        ("icon class",  r'class="[^"]*(?:fa-star|icon-star|star-on|star full)[^"]*"'),
        ("sr-only",     r'class="sr-only">[^<]*stars?'),
        ("words",       r"\b(?:one|two|three|four|five|[1-5])[-\s]?stars?\b"),
        ("bar width",   r'style="width:\s*\d{1,3}%"'),
    ):
        n = len(re.findall(pat, html, re.I))
        if n:
            out.append(f"{label}×{n}")
    return out


def main() -> int:
    session = requests.Session()
    session.headers["User-Agent"] = UA
    for host in sys.argv[1:]:
        base = host if host.startswith("http") else "https://" + host
        base = base.rstrip("/")
        links = edinburgh_links(session, base)
        if not links:
            print(f"  {host:24} no Edinburgh article found by any route")
            continue
        print(f"  {host:24} {len(links)} Edinburgh page(s)")
        for url in links[:2]:
            time.sleep(2)
            page = get(session, url)
            if page is None or page.status_code != 200:
                print(f"      HTTP {getattr(page, 'status_code', 'error')}  {url[-46:]}")
                continue
            found = find_rating(html=page.text, rule={"type": "auto"})
            got = f"{found.stars}* via {found.method}" if found else "extractor: nothing"
            print(f"      {got:24} [{', '.join(signals(page.text)) or 'no signal'}]")
            print(f"        {url[-64:]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
