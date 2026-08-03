"""
The AI half of show matching.

Fuzzy string matching gets most of the way, but it cannot tell that
"Achilles" and "Achilles, Death of the Gods" are the same show while
"Macbeth" and "Macbeth (An Undoing)" are not. That judgement needs to read the
titles the way a person would, which is what this module is for.

It is called for a small minority of reviews — only the ones fuzzy matching
flagged as ambiguous — so the cost stays low even at peak Fringe.

If no API key is configured this module disables itself and the pipeline falls
back to fuzzy matching alone. The leaderboard still builds; it just has more
duplicate rows.
"""

from __future__ import annotations

import json
import os
import sqlite3

from pydantic import BaseModel, Field

# Which Claude model to use. Opus is the most capable and gives the best
# matching accuracy; switch to "claude-sonnet-5" or "claude-haiku-4-5" if you
# want to trade some accuracy for lower cost. Only a small fraction of reviews
# reach this code, so the bill is modest either way — measure before optimising.
# Chosen on evidence, not intuition: run compare_models.py and all three of
# Haiku 4.5, Sonnet 5 and Opus 4.8 were measured against the ten genuinely
# ambiguous pairs from the 2025 data. Haiku and Opus both scored 10/10; Sonnet
# scored 9/10 (it merged "Dream Space" into "Dreamscape"). Haiku is roughly a
# sixth of Opus's cost, so it wins on the evidence available.
#
# Caveat worth remembering: that is ten questions, run once. If duplicate or
# wrongly-merged rows start showing up on the leaderboard, add the offending
# pairs to compare_models.py and re-run before assuming the model is fine.
MODEL = "claude-haiku-4-5"

# Adaptive thinking is not available on every model — Haiku rejects it with a
# 400. Rather than silently dropping it everywhere, name the models that take it
# so switching MODEL above stays a one-line change that actually works.
NO_ADAPTIVE_THINKING = {"claude-haiku-4-5"}

SYSTEM_PROMPT = """\
You match theatre and comedy review headlines to Edinburgh Festival shows.

You are given one review headline and a numbered list of candidate shows already
in the database. Decide whether the headline refers to one of those candidates.

Return the candidate's number, or 0 if the headline is a DIFFERENT show.

Guidance:
- Publications write the same show many ways. "Sam Campbell: Companion Piece",
  "Companion Piece" and "Companion Piece - Pleasance" are all the same show.
- A performer's name attached to a title is still the same show.
- Subtitles are often dropped: "Achilles" and "Achilles, Death of the Gods" are
  very likely the same show.
- But similar names are NOT enough on their own. "Macbeth" and
  "Macbeth (An Undoing)" are different productions. "Roots" and "Roots: A Trip
  Through Time" may well be different shows by different companies.
- Generic or very common titles ("Hamlet", "Cabaret", "Roots") deserve extra
  caution — many companies stage them in the same festival.

When you are genuinely unsure, answer 0.

Answering 0 wrongly creates a duplicate row on a leaderboard, which is untidy.
Answering with a number wrongly attributes someone else's five-star review to
the wrong show, which is a factual error we would have to correct publicly.
The second mistake is much worse than the first. Prefer 0.
"""


ADMISSION_PROMPT = """\
You decide whether a headline is a review of a single show at an Edinburgh
festival: the Fringe, the Free Fringe, the International Festival, or the Art,
Book or Film festivals.

Answer true only if BOTH are true:
- it reviews ONE named show, performance, exhibition or act, and
- that show is on at a festival in EDINBURGH.

Answer false for:
- round-ups and lists ("The best comedy of the Fringe", "Our five-star shows",
  "All our Made in Edinburgh reviews"). These quote many ratings and are the
  main thing this check exists to stop.
- news, previews, announcements, obituaries, cancellations, award reports
  ("Trainspotting tour cancelled", "Fringe announces 2026 line-up").
- interviews and profiles, even of a performer appearing at the festival.
- reviews of shows playing ELSEWHERE - London, Melbourne, Brighton, Camden -
  even when the piece mentions Edinburgh or an upcoming Fringe run.

A show being unfamiliar is not a reason to answer false. Most Fringe shows are
obscure, many are debut hours by unknown acts, and free-fringe shows rarely
appear in any programme. Judge the KIND of article, not whether you recognise
the show.

When genuinely torn, answer false: a held item is reviewed by a person, whereas
a wrongly admitted round-up ranks at the top of the leaderboard with five stars.
"""


class ShowVerdict(BaseModel):
    """Whether a headline is a review of one Edinburgh festival show."""

    is_show: bool = Field(
        description="True only if this reviews one named show at an Edinburgh festival."
    )
    confidence: float = Field(ge=0.0, le=1.0, description="How sure you are.")
    reason: str = Field(description="One short sentence explaining the decision.")


class MatchDecision(BaseModel):
    """The model's answer. Kept deliberately small and explicit."""

    choice: int = Field(
        description="The number of the matching candidate, or 0 for none of them."
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="How sure you are, from 0.0 to 1.0."
    )
    reason: str = Field(description="One short sentence explaining the decision.")


class Matcher:
    """
    Wraps the Anthropic client and a small on-disk cache.

    The cache matters more than it looks: the pipeline re-runs matching whenever
    you fix an alias by hand, and without it every re-run would re-ask (and
    re-pay for) questions already answered.
    """

    def __init__(self, conn: sqlite3.Connection, model: str = MODEL,
                 use_cache: bool = True):
        self.conn = conn
        self.model = model
        self.use_cache = use_cache
        self.client = None
        self.calls = 0
        self.cache_hits = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self._reported_errors: set[str] = set()

        conn.execute(
            """CREATE TABLE IF NOT EXISTS ai_decisions (
                   question   TEXT PRIMARY KEY,
                   choice     INTEGER,
                   confidence REAL,
                   reason     TEXT,
                   decided_at TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )

        # Let the SDK resolve credentials rather than checking for the env var
        # ourselves: it also accepts ANTHROPIC_AUTH_TOKEN and an `ant auth login`
        # profile, and gating on ANTHROPIC_API_KEY alone would silently disable
        # AI matching for anyone using those.
        try:
            import anthropic

            self.client = anthropic.Anthropic()
        except ImportError:
            print("    (anthropic package not installed — AI matching disabled)")
        except Exception as exc:  # noqa: BLE001 - unauthenticated is a normal state
            print(f"    (no Anthropic credentials — AI matching disabled: "
                  f"{type(exc).__name__})")

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def is_festival_show(self, headline: str) -> ShowVerdict | None:
        """
        Is this headline a review of one Edinburgh festival show?

        Asked only for headlines that matched no festival programme, so the call
        volume is a fraction of the day's reviews. Returns None when unavailable,
        which the caller treats as "hold" rather than "admit" — an outage must
        not reopen the gate.
        """
        if not self.enabled:
            return None

        question = json.dumps({"admission": headline})
        cached = self.conn.execute(
            "SELECT choice, confidence, reason FROM ai_decisions WHERE question = ?",
            (question,),
        ).fetchone() if self.use_cache else None
        if cached:
            self.cache_hits += 1
            return ShowVerdict(is_show=bool(cached["choice"]),
                               confidence=cached["confidence"],
                               reason=cached["reason"])

        kwargs = {}
        if self.model not in NO_ADAPTIVE_THINKING:
            kwargs["thinking"] = {"type": "adaptive"}
        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=1000,
                system=ADMISSION_PROMPT,
                messages=[{"role": "user", "content": f"Headline:\n{headline}"}],
                output_format=ShowVerdict,
                **kwargs,
            )
            verdict = response.parsed_output
            self.calls += 1
            self.input_tokens += response.usage.input_tokens
            self.output_tokens += response.usage.output_tokens
        except Exception as exc:  # noqa: BLE001
            self._report(exc)
            return None

        if verdict is not None:
            self.conn.execute(
                """INSERT OR REPLACE INTO ai_decisions
                       (question, choice, confidence, reason) VALUES (?, ?, ?, ?)""",
                (question, int(verdict.is_show), verdict.confidence, verdict.reason),
            )
        return verdict

    def choose(self, headline: str, candidates: list[str]) -> MatchDecision | None:
        """
        Ask which candidate the headline refers to.

        Returns None if AI matching is unavailable or the call failed, so the
        caller can fall back to its own judgement rather than crash the run.
        """
        if not self.enabled or not candidates:
            return None

        question = json.dumps({"headline": headline, "candidates": candidates})

        cached = self.conn.execute(
            "SELECT choice, confidence, reason FROM ai_decisions WHERE question = ?",
            (question,),
        ).fetchone() if self.use_cache else None
        if cached:
            self.cache_hits += 1
            return MatchDecision(
                choice=cached["choice"],
                confidence=cached["confidence"],
                reason=cached["reason"],
            )

        numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(candidates, start=1))
        prompt = f"Review headline:\n{headline}\n\nCandidate shows:\n{numbered}"

        kwargs = {}
        if self.model not in NO_ADAPTIVE_THINKING:
            kwargs["thinking"] = {"type": "adaptive"}

        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=2000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                output_format=MatchDecision,
                **kwargs,
            )
            decision = response.parsed_output
            self.input_tokens += response.usage.input_tokens
            self.output_tokens += response.usage.output_tokens
        except Exception as exc:  # noqa: BLE001 - never let one bad call stop the run
            # Report each distinct failure once. A missing credential or an
            # expired key fails identically on every question, and 500 copies of
            # the same traceback buries anything useful in the run log.
            self._report(exc)
            return None

        if decision is None:
            return None

        # Guard against an out-of-range answer rather than trusting the index.
        if not 0 <= decision.choice <= len(candidates):
            decision = MatchDecision(
                choice=0, confidence=0.0, reason="model returned an invalid index"
            )

        self.calls += 1
        self.conn.execute(
            """INSERT INTO ai_decisions (question, choice, confidence, reason)
               VALUES (?, ?, ?, ?) ON CONFLICT(question) DO NOTHING""",
            (question, decision.choice, decision.confidence, decision.reason),
        )
        return decision

    def _report(self, exc: Exception) -> None:
        """
        Report each distinct failure once.

        A missing credential or an expired key fails identically on every
        question, and 500 copies of the same traceback buries anything useful in
        the run log.
        """
        key = type(exc).__name__
        if key in self._reported_errors:
            return
        self._reported_errors.add(key)
        print(f"      ! AI call failed ({key}): {str(exc)[:140]}")
        print("        (further errors of this kind will be suppressed)")

    def report(self) -> str:
        if not self.enabled:
            return "AI matching disabled"
        return f"{self.calls} AI calls, {self.cache_hits} served from cache"
