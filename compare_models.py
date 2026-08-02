#!/usr/bin/env python3
"""
Run the same ambiguous matching questions past several Claude models and compare.

    export ANTHROPIC_API_KEY=sk-ant-...
    ./.venv/bin/python compare_models.py

Answers the only question that should actually decide which model to use: do the
cheaper ones get YOUR hard cases right? Cost differences across the whole
festival are a few pounds either way, so accuracy is what matters — and this
prints both, using real token counts rather than estimates.

The questions below are the genuinely ambiguous pairs from the August 2025 data,
with the answer a human would give. Add your own as you find them.
"""

from __future__ import annotations

import os
import sys

from src import db
from src.ai_match import Matcher

# (headline, [candidates], expected choice — 0 means "none of these")
QUESTIONS: list[tuple[str, list[str], int]] = [
    ("Joz Norris: You Wait. Time Passes. (Queenie Miller)",
     ["You Wait. Time Passes."], 1),
    ("Joe Tracini: Ten Things I Hate About Me (Joe Tracini and Norwich Theatre)",
     ["Ten Things I Hate About Me"], 1),
    ("Nerds: The Bill Gates vs. Steve Jobs Comedy Musical",
     ["The Bill Gates vs Steve Jobs Comedy Musical (Paul Taylor)"], 1),
    ("Juliet and Romeo", ["Pantomeo and Juliet"], 0),
    ("The Burton Brothers: 1925", ["1902"], 0),
    ("Steffan Alun: Stand Up", ["The Stand"], 0),
    ("MASSAOKE: Sing The Musicals", ["A New Musical"], 0),
    ("Dream Space", ["Dreamscape"], 0),
    # A harder pair worth watching: a real subtitle drop vs a genuine near-miss.
    ("Achilles", ["Achilles, Death of the Gods"], 1),
    ("Macbeth", ["Macbeth (An Undoing)"], 0),
]

# $ per million tokens (input, output). Sonnet 5 is on an introductory rate
# through 2026-08-31, which covers essentially all of the 2026 Fringe.
PRICING = {
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),   # intro rate; reverts to 3.00 / 15.00
    "claude-haiku-4-5": (1.00, 5.00),
}


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY first:\n  export ANTHROPIC_API_KEY=sk-ant-...")
        return 1

    models = sys.argv[1:] or list(PRICING)
    conn = db.connect()
    results: dict[str, dict] = {}

    for model in models:
        print(f"\n=== {model} ===")
        # use_cache=False so each model genuinely answers, rather than reading
        # back an answer another model already gave.
        matcher = Matcher(conn, model=model, use_cache=False)
        if not matcher.enabled:
            print("  client unavailable, skipping")
            continue

        correct = 0
        for headline, candidates, expected in QUESTIONS:
            decision = matcher.choose(headline, candidates)
            got = decision.choice if decision else None
            hit = got == expected
            correct += hit
            mark = "ok  " if hit else "MISS"
            print(f"  {mark} {headline[:46]:<48} want={expected} got={got}")
            if decision and not hit:
                print(f"       reason: {decision.reason}")

        rate_in, rate_out = PRICING.get(model, (0, 0))
        cost = (matcher.input_tokens * rate_in
                + matcher.output_tokens * rate_out) / 1_000_000
        results[model] = {
            "correct": correct,
            "total": len(QUESTIONS),
            "cost": cost,
            "per_call": cost / max(matcher.calls, 1),
        }

    print("\n=== summary ===")
    print(f"  {'model':<20} {'correct':>9} {'cost now':>10} {'per call':>10} {'~festival':>11}")
    for model, r in results.items():
        # Rough festival volume: ~50 ambiguous matches a day over ~25 days.
        festival = r["per_call"] * 1250
        print(f"  {model:<20} {r['correct']}/{r['total']:<7} "
              f"${r['cost']:>9.4f} ${r['per_call']:>9.4f} ${festival:>10.2f}")

    print("\nAccuracy should decide this, not cost — the festival spread is a few pounds.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
