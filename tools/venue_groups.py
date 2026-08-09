"""
Rebuild data/venue-groups.json and data/area-shows.json.

The first maps every festival venue to its area; the second counts how many
shows the programme lists in each area, which is what orders the filter.

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
                                 r"Parliament Sq|Niddry|Blair St|Market St|Mound|"
                                 r"Cockburn|Victoria St|George IV|Chambers St|"
                                 r"Infirmary St|South Bridge|Jeffrey St|Bank St"),
    # The postcode districts are too coarse on their own: EH1 runs from Lothian
    # Road to Greenside Place, and EH3 covers the West End, Stockbridge,
    # Canonmills and Broughton. Naming the streets is what keeps the Traverse
    # out of the Old Town and Stockbridge Church out of Tollcross.
    # Tollcross and the West End are a mile apart and were one group; split on
    # the streets. Lothian Road's theatres — Usher Hall, Lyceum, Traverse — sit
    # with the West End, which is how they are always described; the King's, on
    # Leven Street, is Tollcross.
    ("Tollcross",                r"Home St|Leven St|Bread St|Fountainbridge|"
                                 r"Earl Grey|Lauriston|Tollcross|Gilmore|"
                                 r"Semple|Gardner'?s Cres|Brougham"),
    ("West End",                 r"Lothian Rd|Lothian Road|Cambridge St|Grindlay|"
                                 r"Castle Terr|Morrison|Palmerston|Shandwick|"
                                 r"Haymarket|Torphichen|Manor Pl|Rutland|Belford|"
                                 r"Dean Terr|Atholl|Coates|Melville St|Canning"),
    ("Leith Walk & Broughton",  r"Greenside|Leith Walk|Broughton|Mansfield Pl|"
                                 r"Bellevue|"
                                 r"Picardy|London Rd|Easter Rd|Montgomery St|"
                                 r"Annandale|Brunswick"),
    ("Stockbridge & North West", r"Saxe Coburg|Raeburn|Deanhaugh|Comely Bank|"
                                 r"Canonmills|Henderson Row|Inverleith|Dundas St|"
                                 r"St Stephen"),
    ("New Town",                 r"St James|St Andrew Sq|George St|Rose St|"
                                 r"Hanover|Frederick|Queen St|Thistle St|"
                                 r"Charlotte Sq|Princes St|Dublin St|Howe St|"
                                 r"Castle St|Young St|Hill St|North Bridge|"
                                 r"Cumberland St|Abercromby|St Vincent|Great King|"
                                 r"Drummond Pl|Northumberland|Scotland St|London St|"
                                 r"Heriot Row|India St"),
]

# Everything else falls back to its postcode district, which is still a real
# geography even where the street name is not one we recognise. EH1 folds into
# the Royal Mile group deliberately: splitting "Royal Mile" from "Old Town"
# offered two options nobody could choose between.
DISTRICT = {
    "EH1": "Royal Mile / Old Town", "EH2": "New Town",
    "EH3": "West End", "EH4": "Stockbridge & North West",
    "EH6": "Leith", "EH7": "Leith Walk & Broughton",
    "EH8": "Holyrood, Southside & Pleasance Courtyard", "EH9": "Marchmont & Newington",
    "EH10": "Bruntsfield & Morningside",
    # EH5, EH11, EH12, EH14, EH15 and EH16 are deliberately absent. One or two
    # venues each, miles apart, and an area nobody would choose is worse than no
    # area at all — those venues still appear in the full A-Z list.
}

# EIF publishes no per-venue pages, and there are only twelve of them.
EIF = {
    "Church Hill Theatre": "Bruntsfield & Morningside",
    "Edinburgh Playhouse": "Leith Walk & Broughton",
    "Festival Theatre": "Holyrood, Southside & Pleasance Courtyard",
    "The Hub": "Royal Mile / Old Town",
    "The Lyceum": "West End",
    "Princes Street Gardens (East)": "New Town",
    "The Queen's Hall": "Holyrood, Southside & Pleasance Courtyard",
    "Studio Theatre": "West End",
    "Usher Hall": "West End",
    "King's Theatre": "Tollcross",
    "Playfair Library": "Holyrood, Southside & Pleasance Courtyard",
    }


def area(address: str, postcode: str) -> str:
    """
    The venue's area, from its street address, falling back to its postcode.

    Deliberately does NOT consult the venue's name, which was tried and made
    things worse: it fixed two venues whose address omits the street, and broke
    three whose name merely contains a street word. Broughton High School is in
    Comely Bank, Lauriston Castle is in Cramond, and Meadowbank Sports Centre is
    nowhere near the Meadows.
    """
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
    shows: Counter[str] = Counter()
    for i, url in enumerate(urls, 1):
        page = programme._get(url, timeout=45)
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
            # The venue page carries its whole programme in the Next.js payload,
            # so the number of shows on there is a count of that list. Reading
            # it here avoids fetching 4,300 individual show pages for the same
            # answer.
            payload = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                                page, re.S)
            if payload:
                try:
                    venue = json.loads(payload.group(1))["props"]["pageProps"]["data"]["venue"]
                    shows[group] += len(venue.get("events") or [])
                except (ValueError, KeyError, TypeError):
                    pass
        if i % 50 == 0:
            print(f"  {i}/{len(urls)}")

    for name, group in EIF.items():
        mapping.setdefault(name, group)

    out = ROOT / "data" / "venue-groups.json"
    out.write_text(json.dumps(mapping, indent=1, sort_keys=True, ensure_ascii=False))
    order = ROOT / "data" / "area-shows.json"
    order.write_text(json.dumps(dict(shows.most_common()), indent=1, ensure_ascii=False))

    venues = Counter(mapping.values())
    print(f"\nwrote {out.relative_to(ROOT)} and {order.relative_to(ROOT)}")
    print(f"{len(mapping)} venues, {len(venues)} areas, {sum(shows.values())} shows\n")
    print(f"  {'area':44} {'shows':>6}  venues")
    for group, n in shows.most_common():
        print(f"  {group:44} {n:6}  {venues[group]:6}")


if __name__ == "__main__":
    main()
