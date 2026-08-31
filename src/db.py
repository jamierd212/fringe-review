"""
SQLite storage. The whole database is one file in the repo, committed daily,
which gives us a free audit trail of how the leaderboard changed over time.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "reviews.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS shows (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    performer    TEXT,
    festival     TEXT,
    year         INTEGER,
    edfringe_url TEXT,
    genre        TEXT,
    subgenre     TEXT,
    venue        TEXT,
    start_time   TEXT,      -- "HH:MM", the show's usual start
    duration     TEXT       -- minutes, as the programme states it
);

CREATE TABLE IF NOT EXISTS aliases (
    alias   TEXT NOT NULL,
    show_id TEXT NOT NULL REFERENCES shows(id),
    PRIMARY KEY (alias, show_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    url          TEXT PRIMARY KEY,
    show_id      TEXT REFERENCES shows(id),
    publication  TEXT NOT NULL,
    headline     TEXT,
    stars        INTEGER,
    original     TEXT,
    converted    INTEGER DEFAULT 0,
    rounded      INTEGER DEFAULT 0,
    published    TEXT,
    confidence   REAL,
    method       TEXT,
    -- Who wrote it, where the source names them. EdFringeReview runs on
    -- volunteer critics: two of them reviewing the same show is ordinary and
    -- both verdicts are worth showing, while the same one reviewing it twice is
    -- a duplicate. Only a name separates those.
    reviewer     TEXT,
    first_seen   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS seen (
    url        TEXT PRIMARY KEY,
    checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
    outcome    TEXT
);

-- Reviews refused admission to the leaderboard. Kept rather than deleted so a
-- wrong rejection is visible and reversible, and so the same headline is not
-- re-sent to the model on every run.
-- Whether a published link still leads to the review it claims to. A link can
-- return 200 and be empty (see The List), so status alone proves nothing.
CREATE TABLE IF NOT EXISTS link_checks (
    url        TEXT PRIMARY KEY,
    checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
    ok         INTEGER,
    detail     TEXT
);

-- What each source's discovery actually returned, and the last HTTP status the
-- runner saw doing it. One row per source per run, so a source that behaves
-- differently on the Action than on a laptop leaves evidence rather than
-- requiring someone to be watching the log at the time.
CREATE TABLE IF NOT EXISTS source_probes (
    publication TEXT NOT NULL,
    ran_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    status      INTEGER,
    found       INTEGER
);

-- Where a company says it can be found, taken from its own programme entry.
-- Not guessed: a handle inferred from a show's name reaches whoever holds it.
CREATE TABLE IF NOT EXISTS socials (
    show_id  TEXT NOT NULL,
    network  TEXT NOT NULL,
    handle   TEXT NOT NULL,
    PRIMARY KEY (show_id, network)
);

CREATE TABLE IF NOT EXISTS holds (
    url         TEXT PRIMARY KEY,
    headline    TEXT,
    publication TEXT,
    reason      TEXT,
    decided_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_reviews_show ON reviews(show_id);

-- One review of one show from one publication, whatever the URL happens to look
-- like that day. The primary key is the URL, which does not catch a review that
-- arrives twice under two spellings of the same address: http against https, a
-- trailing slash, a www, %E2%98%85 against %e2%98%85, or a round-up article
-- collected whole and later split into its shows. Each of those got counted as
-- a second opinion and moved a show up the board.
--
-- The fragment is deliberately kept: it is what separates the shows inside a
-- round-up, so #bigfoot and #jolly-fisherman are genuinely different reviews.
-- It is only dropped for the comparison when one side has no fragment at all,
-- which is the whole-article row a split has replaced.
--
-- Enforced here rather than in the collector because four different things
-- write reviews — the sweep, tools/add_reviews.py, the backfill and any script
-- written in a hurry — and only the database sees all of them.
CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_once
    ON reviews (show_id, publication,
                rtrim(replace(replace(replace(
                    lower(substr(url, 1, instr(url || '#', '#') - 1)),
                    'https://', ''), 'http://', ''), 'www.', ''), '/'),
                CASE WHEN instr(url, '#') > 0
                     THEN lower(substr(url, instr(url, '#') + 1)) ELSE '' END);
-- And once per critic, whatever the address. A publication can reissue a review
-- under a new URL, or hold two ids for the same piece — EdFringeReview had one
-- review of Elvis in Chaos stored twice that way, same critic, same date, same
-- three stars, two links. The URL rule above cannot see that: the addresses are
-- genuinely different.
--
-- Only where a reviewer is actually recorded, which today means EdFringeReview
-- alone: everywhere else the column is NULL, and NULLs do not collide in SQLite,
-- so this constrains exactly the reviews that carry the evidence for it. Two
-- different critics at the same publication reviewing the same show remains a
-- second opinion and is still counted, which is the whole point of keeping the
-- reviewer at all.
CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_one_per_critic
    ON reviews (show_id, publication, reviewer)
    WHERE reviewer IS NOT NULL AND TRIM(reviewer) <> '';

CREATE INDEX IF NOT EXISTS idx_aliases_alias ON aliases(alias);
"""


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(shows)")}
    for name in ("genre", "subgenre", "venue", "start_time", "duration",
                 "presented_by"):
        if name not in have:
            conn.execute(f"ALTER TABLE shows ADD COLUMN {name} TEXT")
    have = {r[1] for r in conn.execute("PRAGMA table_info(reviews)")}
    if "reviewer" not in have:
        conn.execute("ALTER TABLE reviews ADD COLUMN reviewer TEXT")


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    return conn


def already_seen(conn: sqlite3.Connection, url: str) -> bool:
    return conn.execute("SELECT 1 FROM seen WHERE url = ?", (url,)).fetchone() is not None


def mark_seen(conn: sqlite3.Connection, url: str, outcome: str) -> None:
    conn.execute(
        "INSERT INTO seen (url, outcome) VALUES (?, ?) "
        "ON CONFLICT(url) DO UPDATE SET outcome = excluded.outcome",
        (url, outcome),
    )


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    def count(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    return {
        "shows": count("SELECT COUNT(*) FROM shows"),
        "reviews": count("SELECT COUNT(*) FROM reviews"),
        "rated": count("SELECT COUNT(*) FROM reviews WHERE stars IS NOT NULL"),
        "seen": count("SELECT COUNT(*) FROM seen"),
    }


def record_probe(conn: sqlite3.Connection, publication: str,
                 status: int | None, found: int) -> None:
    """Record what discovery saw for one source on one run."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS source_probes (publication TEXT NOT NULL, "
        "ran_at TEXT DEFAULT CURRENT_TIMESTAMP, status INTEGER, found INTEGER)")
    conn.execute("INSERT INTO source_probes (publication, status, found) "
                 "VALUES (?, ?, ?)", (publication, status, found))
    # Two weeks is enough to see a pattern and short enough that the table never
    # becomes a second copy of the run history.
    conn.execute("DELETE FROM source_probes "
                 "WHERE ran_at < datetime('now', '-14 days')")
    conn.commit()
