"""
Rebuild data/venue-groups.json: every festival venue, mapped to its area.

Run once a season, not once a day — it fetches about 300 venue pages at one
request a second, and venues change yearly rather than daily:

    python tools/venue_groups.py

Areas come from the venue's own published address, not from its name. That
matters: Pleasance Grassmarket is in the Grassmarket, not with the other
Pleasance venues, and grouping by name would put it in the wrong place for
anyone deciding what they can walk to.
"""

from __future__ import annotations

import html
import json
import pathlib
import re
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src import programme  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Matched against the street address, in order; the first hit wins. These are
# the clusters people name when they say where they are going.
NAMED = [
    # "Grass Market" as two words is how some venues write it; missing that
    # sent Pleasance Grassmarket to the West End on a postcode fallback.
    ("Grassmarket",              r"Grass\s?market|West Port|Candlemaker"),
    ("George Square",            r"George Sq"),
    ("Bristo Square & Teviot",   r"Bristo|Teviot"),
    ("Cowgate",                  r"Cowgate"),
    ("Summerhall & The Meadows", r"Summerhall|Hope Park|Meadow|Sciennes|Buccleuch"),
    ("Royal Mile / Old Town",    r"High St|Royal Mile|Canongate|Lawnmarket|"
                                 r"Parliament Sq|Niddry|Blair St|Market St|Mound"),
]

# Everything else falls back to its postcode district, which is still a real
# geography even where the street name is not one we recognise. EH1 folds into
# the Royal Mile group deliberately: splitting "Royal Mile" from "Old Town"
# offered two options nobody could choose between.
DISTRICT = {
    "EH1": "Royal Mile / Old Town", "EH2": "New Town",
    "EH3": "West End & Tollcross", "EH4": "Stockbridge & North West",
    "EH5": "North Edinburgh", "EH6": "Leith", "EH7": "Broughton & Leith Walk",
    "EH8": "Southside & Holyrood", "EH9": "Marchmont & Newington",
    "EH10": "Bruntsfield & Morningside", "EH11": "Gorgie & Dalry",
    "EH12": "West Edinburgh", "EH14": "South West Edinburgh",
    "EH15": "Portobello", "EH16": "South Edinburgh",
}

# EIF publishes no per-venue pages, and there are only twelve of them.
EIF = {
    "Church Hill Theatre": "Bruntsfield & Morningside",
    "Edinburgh Playhouse": "Broughton & Leith Walk",
    "Festival Theatre": "Southside & Holyrood",
    "The Hub": "Royal Mile / Old Town",
    "The Lyceum": "West End & Tollcross",
    "Princes Street Gardens (East)": "New Town",
    "The Queen's Hall": "Southside & Holyrood",
    "Studio Theatre": "West End & Tollcross",
    "Usher Hall": "West End & Tollcross",
    "King's Theatre": "West End & Tollcross",
    "Playfair Library": "Southside & Holyrood",
    "Space @ The Broomhouse Hub": "West Edinburgh",
}


def area(address: str, postcode: str) -> str:
    for label, pattern in NAMED:
        if re.search(pattern, address, re.I):
            return label
    return DISTRICT.get(postcode.split()[0] if postcode else "", "")


def main() -> None:
    sitemap = programme._get("https://www.edfringe.com/tickets/sitemap.xml", timeout=60)
    urls = sorted({
        u for u in re.findall(r"<loc>\s*(.*?)\s*</loc>", sitemap or "", re.S)
        if "/tickets/venues/" in u and not u.endswith("/map")
    })
    print(f"{len(urls)} Fringe venue pages")

    mapping: dict[str, str] = {}
    for i, url in enumerate(urls, 1):
        page = programme._get(url, timeout=30)
        if not page:
            continue
        name = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
        if not name:
            continue
        # Entities survive here because the name is read straight out of the
        # HTML rather than through a parser.
        name = html.unescape(" ".join(re.sub(r"<[^>]+>", "", name.group(1)).split()))
        postcode = re.search(r"\bEH\d{1,2}\s?\d[A-Z]{2}\b", page)
        address = re.findall(r'"address[^"]*"\s*:\s*"([^"]{3,70})"', page)
        group = area(html.unescape(address[0]) if address else "",
                     postcode.group(0) if postcode else "")
        if group:
            mapping[name] = group
        if i % 50 == 0:
            print(f"  {i}/{len(urls)}")

    for name, group in EIF.items():
        mapping.setdefault(name, group)

    out = ROOT / "data" / "venue-groups.json"
    out.write_text(json.dumps(mapping, indent=1, sort_keys=True, ensure_ascii=False))
    counts = Counter(mapping.values())
    print(f"\nwrote {out.relative_to(ROOT)}: {len(mapping)} venues, {len(counts)} areas")
    for group, n in counts.most_common():
        print(f"  {group:28} {n:3}")


if __name__ == "__main__":
    main()
