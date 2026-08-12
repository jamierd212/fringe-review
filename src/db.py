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

CREATE TABLE IF NOT EXISTS holds (
    url         TEXT PRIMARY KEY,
    headline    TEXT,
    publication TEXT,
    reason      TEXT,
    decided_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_reviews_show ON reviews(show_id);
CREATE INDEX IF NOT EXISTS idx_aliases_alias ON aliases(alias);
"""


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(shows)")}
    for name in ("genre", "subgenre", "venue", "start_time", "duration"):
        if name not in have:
            conn.execute(f"ALTER TABLE shows ADD COLUMN {name} TEXT")


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
