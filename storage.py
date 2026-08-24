"""SQLite persistence: daily snapshots, watchlist history, seen-IPOs.

The snapshot store is the heart of the bot — the GSE API has no history endpoint,
so momentum/volume-trend/score-over-time can only be computed from rows we save here.
Start capturing from day one.
"""
import sqlite3
from contextlib import contextmanager
from datetime import date

DB_PATH = "gse.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    date    TEXT NOT NULL,
    symbol  TEXT NOT NULL,
    price   REAL,
    change  REAL,
    volume  REAL,
    eps     REAL,
    dps     REAL,
    capital REAL,
    shares  REAL,
    sector  TEXT,
    PRIMARY KEY (date, symbol)
);

CREATE TABLE IF NOT EXISTS watchlist (
    date           TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    score          REAL,
    pe             REAL,
    div_yield      REAL,
    momentum       REAL,
    liquidity_flag INTEGER,
    rank           INTEGER,
    PRIMARY KEY (date, symbol)
);

CREATE TABLE IF NOT EXISTS ipo_seen (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT,
    title      TEXT,
    url        TEXT,
    company    TEXT,
    first_seen TEXT,
    sent       INTEGER DEFAULT 0
);
"""


@contextmanager
def connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path=DB_PATH):
    """Create tables if they don't exist. Idempotent."""
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def save_snapshot(rows, snapshot_date=None, db_path=DB_PATH):
    """Upsert one row per symbol for the given date (defaults to today).

    Returns the number of rows written.
    """
    snapshot_date = snapshot_date or date.today().isoformat()
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO snapshots
                (date, symbol, price, change, volume, eps, dps, capital, shares, sector)
            VALUES
                (:date, :symbol, :price, :change, :volume, :eps, :dps, :capital, :shares, :sector)
            ON CONFLICT(date, symbol) DO UPDATE SET
                price=excluded.price, change=excluded.change, volume=excluded.volume,
                eps=excluded.eps, dps=excluded.dps, capital=excluded.capital,
                shares=excluded.shares, sector=excluded.sector
            """,
            [{**r, "date": snapshot_date} for r in rows],
        )
    return len(rows)


def latest_snapshot_date(db_path=DB_PATH):
    with connect(db_path) as conn:
        row = conn.execute("SELECT MAX(date) AS d FROM snapshots").fetchone()
        return row["d"] if row else None


def snapshot_count(snapshot_date=None, db_path=DB_PATH):
    snapshot_date = snapshot_date or date.today().isoformat()
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM snapshots WHERE date = ?", (snapshot_date,)
        ).fetchone()
        return row["n"]


def get_snapshot(snapshot_date, db_path=DB_PATH):
    """All rows for one date, as a list of dicts."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM snapshots WHERE date = ? ORDER BY symbol", (snapshot_date,)
        ).fetchall()
        return [dict(r) for r in rows]


def last_known_fundamentals(symbol, as_of_date, db_path=DB_PATH):
    """Most recent non-null (eps, dps) for a symbol on/before `as_of_date`.

    Fundamentals only change on earnings/dividend announcements, so carrying the
    last reported figures forward is correct (that's what a trailing P/E / yield
    is). Lets value/income keep scoring when the fundamentals source is down.
    Searches ALL history, not just the recent window (afx can be down a while).
    """
    with connect(db_path) as conn:
        eps = conn.execute(
            "SELECT eps FROM snapshots WHERE symbol=? AND date<=? AND eps IS NOT NULL "
            "ORDER BY date DESC LIMIT 1", (symbol, as_of_date)).fetchone()
        dps = conn.execute(
            "SELECT dps FROM snapshots WHERE symbol=? AND date<=? AND dps IS NOT NULL "
            "ORDER BY date DESC LIMIT 1", (symbol, as_of_date)).fetchone()
        return (eps["eps"] if eps else None, dps["dps"] if dps else None)


def get_symbol_history(symbol, limit, db_path=DB_PATH):
    """Most-recent `limit` snapshots for one symbol, oldest-first."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM snapshots WHERE symbol = ? ORDER BY date DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


def distinct_snapshot_dates(db_path=DB_PATH):
    """All snapshot dates, oldest-first."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM snapshots ORDER BY date"
        ).fetchall()
        return [r["date"] for r in rows]


def save_watchlist(rows, run_date=None, db_path=DB_PATH):
    """Upsert the screener's ranked output for a date. Returns rows written."""
    run_date = run_date or date.today().isoformat()
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO watchlist
                (date, symbol, score, pe, div_yield, momentum, liquidity_flag, rank)
            VALUES
                (:date, :symbol, :score, :pe, :div_yield, :momentum, :liquidity_flag, :rank)
            ON CONFLICT(date, symbol) DO UPDATE SET
                score=excluded.score, pe=excluded.pe, div_yield=excluded.div_yield,
                momentum=excluded.momentum, liquidity_flag=excluded.liquidity_flag,
                rank=excluded.rank
            """,
            [{**r, "date": run_date} for r in rows],
        )
    return len(rows)


def get_watchlist(run_date, db_path=DB_PATH):
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM watchlist WHERE date = ? ORDER BY rank", (run_date,)
        ).fetchall()
        return [dict(r) for r in rows]


def previous_watchlist_date(before_date, db_path=DB_PATH):
    """Most recent watchlist date strictly before `before_date`, or None."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(date) AS d FROM watchlist WHERE date < ?", (before_date,)
        ).fetchone()
        return row["d"] if row and row["d"] else None


def ipo_seen_keys(db_path=DB_PATH):
    """Set of stored dedupe keys (normalized 'title|company') for the IPO watcher."""
    with connect(db_path) as conn:
        rows = conn.execute("SELECT title, company FROM ipo_seen").fetchall()
        return {f"{(r['title'] or '').strip().lower()}|{(r['company'] or '').strip().lower()}"
                for r in rows}


def add_ipo(source, title, url, company, first_seen, sent=0, db_path=DB_PATH):
    """Record a newly-seen IPO candidate. Returns its row id."""
    with connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO ipo_seen (source, title, url, company, first_seen, sent)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (source, title, url, company, first_seen, sent),
        )
        return cur.lastrowid


def recent_ipos(limit=50, db_path=DB_PATH):
    """Most-recently-seen IPO candidates, newest first."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM ipo_seen ORDER BY first_seen DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def watchlist_dates(db_path=DB_PATH):
    """All watchlist run dates, newest first."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM watchlist ORDER BY date DESC"
        ).fetchall()
        return [r["date"] for r in rows]
