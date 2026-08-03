"""
Deciding what is allowed onto the leaderboard.

The problem this solves: a round-up article quotes everybody else's five-star
verdicts, so the rating cascade scores the round-up itself 5 stars and invents a
show called "All #EdFringe 2025 Made in Edinburgh reviews" — which then ranks at
position one, the most visible slot on the site. Reviews of shows in London that
happen to mention Edinburgh do the same.

Keyword rules are the obvious fix and the wrong one. Tested against the 2025
data they flagged 19% of entries, and the flags were "The 39 Steps", "The Dating
Party Game Show", "A Broken Man's Guide to Fixing Others" — real shows. Tighten
them enough to catch the junk and they start deleting the festival.

So admission is decided on evidence instead, in two tiers:

    1. The show appears in a festival's own programme. Authoritative, free, and
       impossible to fake: an article about the festival is never listed as a
       show in it.

    2. It does not — which is common and innocent. Only 31% of PBH Free Fringe
       shows appear in the official Fringe programme, and those acts are the
       least likely to have press elsewhere, so a programme-only gate would
       quietly delete exactly the shows that need the exposure. These go to the
       model, which is asked one narrow question it is well suited to.

Anything failing both is held, not discarded, so the false-rejection rate stays
visible rather than becoming silent data loss.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from rapidfuzz import fuzz, process

from . import programme

# The same band show-to-show matching uses. Below this, a "match" is usually two
# different shows sharing a common word.
ACCEPT = 92

# Fuzzy matching on a very short alias is noise: "pleasance" scores well against
# any programme entry containing it. Exact hits are still honoured at any length.
MIN_FUZZY_LEN = 8


@dataclass
class Verdict:
    admit: bool
    tier: str                 # "programme" | "ai" | "held"
    reason: str
    festival: str | None = None
    url: str | None = None


class Gatekeeper:
    """Holds the combined programme index and answers one question about a headline."""

    def __init__(self, matcher=None, index: dict | None = None):
        self.index = programme.known_shows() if index is None else index
        self.keys = list(self.index)
        self.matcher = matcher
        self.counts: Counter = Counter()
        self.last_verdict: Verdict | None = None

    def verdict(self, headline: str, aliases: list[str]) -> Verdict:
        found = self._in_programme(aliases)
        if found:
            key, festival, url = found
            self.counts["programme"] += 1
            return Verdict(True, "programme", f"listed by {festival}: {key}",
                           festival=festival, url=url)

        if self.matcher is not None and self.matcher.enabled:
            decision = self.matcher.is_festival_show(headline)
            if decision is not None:
                if decision.is_show:
                    self.counts["ai"] += 1
                    return Verdict(True, "ai", decision.reason)
                self.counts["held"] += 1
                return Verdict(False, "held", decision.reason)

        # No programme entry and no model available. Admitting would reopen the
        # hole; rejecting outright would throw away real shows on a day the API
        # is down. Hold it, so the decision is deferred to a person.
        self.counts["held"] += 1
        return Verdict(False, "held", "not in any programme; no AI check available")

    def _in_programme(self, aliases: list[str]) -> tuple[str, str, str] | None:
        for alias in aliases:
            hit = self.index.get(alias)
            if hit:
                return alias, hit[0], hit[1]

        for alias in aliases:
            if len(alias) < MIN_FUZZY_LEN:
                continue
            match = process.extractOne(alias, self.keys, scorer=fuzz.token_sort_ratio,
                                       score_cutoff=ACCEPT)
            if match:
                festival, url = self.index[match[0]]
                return match[0], festival, url
        return None
