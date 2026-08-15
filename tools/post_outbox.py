"""
Read the outbox, show it, and post it to Bluesky once a person says so.

Deliberately separate from the daily run, and deliberately not automatic. A post
is public the instant it is made and cannot be recalled; a sweep that goes wrong
at seven in the morning should not be able to publish before anyone has read what
it wrote.

    python tools/post_outbox.py                 # show what is waiting, send nothing
    python tools/post_outbox.py --send          # ask, then post, then clear

Credentials come from the environment, never from the repository:

    BLUESKY_HANDLE    e.g. fringestars.bsky.social
    BLUESKY_PASSWORD  an APP password from Settings -> App Passwords,
                      not the account password

An app password can be revoked on its own and cannot change the account's own
password, which is the right shape of key to leave lying around in a shell.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.standings import OUTBOX                        # noqa: E402

API = "https://bsky.social/xrpc"
LIMIT = 300                     # Bluesky's own limit, in graphemes


def load() -> list[dict]:
    if not OUTBOX.exists():
        return []
    try:
        return json.loads(OUTBOX.read_text())
    except ValueError:
        print("The outbox is not readable JSON; leaving it alone.")
        return []


def session(handle: str, password: str) -> dict:
    r = requests.post(f"{API}/com.atproto.server.createSession",
                      json={"identifier": handle, "password": password}, timeout=30)
    r.raise_for_status()
    return r.json()


def post(auth: dict, text: str) -> str:
    """Post one message. Returns its URI."""
    # The link is included as a facet so it is clickable rather than bare text.
    facets = []
    start = text.find("https://")
    if start >= 0:
        end = len(text.encode()) if text.endswith("/") else len(text)
        facets = [{
            "index": {"byteStart": len(text[:start].encode()),
                      "byteEnd": len(text[:end].encode())},
            "features": [{"$type": "app.bsky.richtext.facet#link",
                          "uri": text[start:end].strip()}],
        }]
    from datetime import datetime, timezone
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if facets:
        record["facets"] = facets
    r = requests.post(
        f"{API}/com.atproto.repo.createRecord",
        headers={"Authorization": f"Bearer {auth['accessJwt']}"},
        json={"repo": auth["did"], "collection": "app.bsky.feed.post", "record": record},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("uri", "")


def main() -> int:
    notes = load()
    if not notes:
        print("Nothing waiting.")
        return 0

    print(f"{len(notes)} message(s) waiting:\n")
    for n in notes:
        over = len(n["text"]) - LIMIT
        flag = f"   !! {over} characters too long" if over > 0 else ""
        if flag:
            print(f"  {flag.strip()}")
        print("  " + n["text"].replace("\n", "\n  "))
        print()

    if "--send" not in sys.argv:
        print("Nothing sent. Re-run with --send to post these.")
        return 0

    handle = os.environ.get("BLUESKY_HANDLE")
    password = os.environ.get("BLUESKY_PASSWORD")
    if not handle or not password:
        print("Set BLUESKY_HANDLE and BLUESKY_PASSWORD first. Nothing sent.")
        return 1

    answer = input(f"Post all {len(notes)} to {handle}? Type yes to confirm: ")
    if answer.strip().lower() != "yes":
        print("Nothing sent.")
        return 0

    auth = session(handle, password)
    sent = []
    for n in notes:
        if len(n["text"]) > LIMIT:
            print(f"  skipped (too long): {n['title'][:40]}")
            continue
        try:
            uri = post(auth, n["text"])
            print(f"  posted: {n['title'][:40]}  {uri[-20:]}")
            sent.append(n["show_id"])
        except requests.RequestException as exc:
            # Stop at the first failure rather than hammering: whatever went
            # wrong for one is likely to go wrong for the rest, and the
            # unsent ones stay in the outbox for the next attempt.
            print(f"  FAILED on {n['title'][:40]}: {exc}")
            break

    remaining = [n for n in notes if n["show_id"] not in sent]
    OUTBOX.write_text(json.dumps(remaining, indent=2))
    print(f"\n{len(sent)} sent, {len(remaining)} left in the outbox.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
