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
    subgenre     TEXT
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

CREATE INDEX IF NOT EXISTS idx_reviews_show ON reviews(show_id);
CREATE INDEX IF NOT EXISTS idx_aliases_alias ON aliases(alias);
"""


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(shows)")}
    for name in ("genre", "subgenre"):
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
