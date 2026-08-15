"""
Assess a publication for the leaderboard: may we, can we find it, can we read it.

Written after five sources in a row were wrongly written off, each for a
different reason and none of them because the publication was unclear. The
lessons are built in:

  - a dead feed can sit in front of a live site, so several ways in are tried
  - a feed's URL often contains "review", so a feed is never read as an article
  - the rating may be in an image filename, an alt attribute, a screen-reader
    label, an icon font or a bar width, so all of those are looked for
  - and the constant that looks like a working extractor: if every article
    scores the same, that is reported, because it usually means the number came
    from the page furniture rather than the review

It prints what it found. It does not decide.
"""

from __future__ import annotations

import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ratings import find_rating                   # noqa: E402

UA = "FringeLeaderboardBot/0.1 (+https://fringestars.com/bot.html)"
TOKEN = UA.split("/")[0]
EDINBURGH = re.compile(r"edinburgh|fringe|edfringe", re.I)
ASSET = re.compile(r"\.(?:css|js|woff2?|png|jpe?g|svg|gif|ico|xml|json)(?:\?|$)", re.I)


def get(session, url):
    try:
        return session.get(url, timeout=20, allow_redirects=True)
    except requests.RequestException:
        return None


def may_we(session, base):
    r = get(session, base + "/robots.txt")
    if r is None:
        return "unreachable", False
    if r.status_code in (401, 403):
        return f"refuses robots.txt ({r.status_code})", False
    if r.status_code != 200:
        return "no robots.txt", True
    parser = RobotFileParser()
    parser.parse(r.text.splitlines())
    ok = parser.can_fetch(TOKEN, base + "/a-review/")
    delay = parser.crawl_delay(TOKEN)
    return ("allows us" + (f", {delay}s delay" if delay else "")) if ok else "DISALLOWS us", ok


def articles(session, base, want=6):
    """Edinburgh article URLs, from whichever way in works."""
    entries = [base, f"{base}/reviews", f"{base}/reviews/", f"{base}/edinburgh",
               f"{base}/edinburgh-fringe", f"{base}/tag/edinburgh-fringe/",
               f"{base}/category/edinburgh-fringe/", f"{base}/feed/"]
    found: list[str] = []
    for entry in entries:
        page = get(session, entry)
        if page is None or page.status_code != 200:
            continue
        for href in re.findall(r'href="([^"#]+)"', page.text):
            url = urljoin(entry, href)
            if not url.startswith(base) or ASSET.search(url):
                continue
            if url.rstrip("/") in (base, entry.rstrip("/")):
                continue
            # An article has a slug; a section index does not.
            if EDINBURGH.search(url) and re.search(r"/[a-z0-9][a-z0-9-]{18,}/?$", url):
                if url not in found:
                    found.append(url)
        if len(found) >= want:
            break
    return found[:want]


def signals(html):
    out = []
    for label, pat in (
        ("chars",     r"[★⭐✭✦]"),
        ("img-name",  r'<img[^>]+src="[^"]*\d[-_ ]?stars?\b[^"]*"'),
        ("img-alt",   r'<img[^>]+alt="[^"]*\bstars?\b[^"]*"'),
        ("jsonld",    r'"ratingValue"'),
        ("microdata", r'itemprop="ratingValue"'),
        ("icons",     r'class="[^"]*(?:fa-star|icon-star|star-on|stars?-\d)[^"]*"'),
        ("sr-only",   r'class="[^"]*sr-only[^"]*">[^<]*stars?'),
        ("words",     r"\b(?:one|two|three|four|five|[1-5])[-\s]?stars?\b"),
        ("bar width", r'style="width:\s*\d{1,3}%'),
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
        base = f"{urlsplit(base).scheme}://{urlsplit(base).netloc}"
        verdict, ok = may_we(session, base)
        if not ok:
            print(f"  {host:28} {verdict}")
            continue
        urls = articles(session, base)
        if not urls:
            print(f"  {host:28} {verdict}; no Edinburgh article found")
            continue
        scores, seen_signals = Counter(), set()
        for url in urls:
            time.sleep(1.5)
            page = get(session, url)
            if page is None or page.status_code != 200:
                continue
            found = find_rating(html=page.text, rule={"type": "auto"})
            scores[found.stars if found else None] += 1
            seen_signals.update(signals(page.text))
        rated = {k: v for k, v in scores.items() if k is not None}
        spread = ", ".join(f"{k}*×{v}" for k, v in sorted(rated.items()))
        note = ""
        if len(rated) == 1 and sum(rated.values()) > 2:
            note = "  <-- SAME SCORE EVERY TIME, likely page furniture"
        print(f"  {host:28} {verdict}; {len(urls)} article(s); "
              f"{spread or 'no rating extracted'}{note}")
        print(f"      signals: {', '.join(sorted(seen_signals)) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
