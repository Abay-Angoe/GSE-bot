"""Pan-African value screener + IPO tracker (isolated from the GSE bot).

Scrapes afx.kwayisi.org (the same provider as the GSE fundamentals, so the page
layout and parser are shared) across several African exchanges into its own
`africa.db`. Produces a daily "low value / high potential" watchlist using the
same transparent, renormalized balanced-blend scoring as the GSE screener, and
keeps a pan-African IPO/listing table.

Heavy job: hundreds of pages. Run once daily, sequentially, with a polite delay.
Fundamentals coverage varies by exchange (e.g. Kenya rich, Nigeria sparse); the
per-symbol weight renormalization means a stock simply scores on whatever factors
it has, so uneven coverage degrades gracefully.

Cross-exchange caveat: factors are normalized across the whole pan-African
universe, so "cheap" is measured Africa-wide, not per local market. It's a
flagging tool, not an apples-to-apples valuation.
"""
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import date

import requests
from bs4 import BeautifulSoup

import config
import ipo_watcher
from screener import _normalize  # reuse the 0-100 normalizer

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GSE-Bot/1.0)"}
_WS = re.compile(r"\s+")
_SUFFIX = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12, "TR": 1e12}

SCHEMA = """
CREATE TABLE IF NOT EXISTS africa_snapshots (
    date       TEXT NOT NULL,
    exchange   TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    name       TEXT,
    price      REAL,
    change_pct REAL,
    volume     REAL,
    eps        REAL,
    dps        REAL,
    pe         REAL,
    yld        REAL,
    sector     TEXT,
    trade_date TEXT,
    PRIMARY KEY (date, exchange, symbol)
);

CREATE TABLE IF NOT EXISTS africa_watchlist (
    date           TEXT NOT NULL,
    exchange       TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    name           TEXT,
    score          REAL,
    pe             REAL,
    yld            REAL,
    momentum       REAL,
    liquidity_flag INTEGER,
    rank           INTEGER,
    PRIMARY KEY (date, exchange, symbol)
);

CREATE TABLE IF NOT EXISTS africa_ipos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange   TEXT,
    country    TEXT,
    source     TEXT,
    title      TEXT,
    url        TEXT,
    first_seen TEXT,
    sent       INTEGER DEFAULT 0
);
"""


# --------------------------------------------------------------- storage ----

@contextmanager
def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path):
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def save_snapshots(rows, snap_date, db_path):
    with connect(db_path) as conn:
        conn.executemany(
            """INSERT INTO africa_snapshots
                 (date,exchange,symbol,name,price,change_pct,volume,eps,dps,pe,yld,sector,trade_date)
               VALUES
                 (:date,:exchange,:symbol,:name,:price,:change_pct,:volume,:eps,:dps,:pe,:yld,:sector,:trade_date)
               ON CONFLICT(date,exchange,symbol) DO UPDATE SET
                 name=excluded.name, price=excluded.price, change_pct=excluded.change_pct,
                 volume=excluded.volume, eps=excluded.eps, dps=excluded.dps, pe=excluded.pe,
                 yld=excluded.yld, sector=excluded.sector, trade_date=excluded.trade_date""",
            [{**r, "date": snap_date} for r in rows],
        )
    return len(rows)


def symbol_history(exchange, symbol, limit, db_path):
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM africa_snapshots WHERE exchange=? AND symbol=?
               ORDER BY date DESC LIMIT ?""",
            (exchange, symbol, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


def get_snapshot(snap_date, db_path):
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM africa_snapshots WHERE date=?", (snap_date,)
        ).fetchall()
        return [dict(r) for r in rows]


def save_watchlist(rows, run_date, db_path):
    with connect(db_path) as conn:
        # A watchlist is a full re-rank, not incremental: clear the day first so a
        # changed universe (e.g. a smaller test run) can't leave stale/duplicate ranks.
        conn.execute("DELETE FROM africa_watchlist WHERE date = ?", (run_date,))
        conn.executemany(
            """INSERT INTO africa_watchlist
                 (date,exchange,symbol,name,score,pe,yld,momentum,liquidity_flag,rank)
               VALUES
                 (:date,:exchange,:symbol,:name,:score,:pe,:yld,:momentum,:liquidity_flag,:rank)
               ON CONFLICT(date,exchange,symbol) DO UPDATE SET
                 name=excluded.name, score=excluded.score, pe=excluded.pe, yld=excluded.yld,
                 momentum=excluded.momentum, liquidity_flag=excluded.liquidity_flag,
                 rank=excluded.rank""",
            [{**r, "date": run_date} for r in rows],
        )
    return len(rows)


def get_watchlist(run_date, db_path, limit=None):
    q = "SELECT * FROM africa_watchlist WHERE date=? ORDER BY rank"
    if limit:
        q += f" LIMIT {int(limit)}"
    with connect(db_path) as conn:
        return [dict(r) for r in conn.execute(q, (run_date,)).fetchall()]


def ipo_keys(db_path):
    with connect(db_path) as conn:
        rows = conn.execute("SELECT title FROM africa_ipos").fetchall()
        return {(r["title"] or "").strip().lower() for r in rows}


def add_ipo(exchange, country, source, title, url, first_seen, db_path):
    with connect(db_path) as conn:
        conn.execute(
            """INSERT INTO africa_ipos (exchange,country,source,title,url,first_seen)
               VALUES (?,?,?,?,?,?)""",
            (exchange, country, source, title, url, first_seen),
        )


def recent_ipos(limit, db_path):
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM africa_ipos ORDER BY first_seen DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------- scraping ----

def _num(s, suffixed=False):
    """Parse a number, optionally with a K/M/B/Tr magnitude suffix."""
    if s is None:
        return None
    s = s.replace(",", "").strip()
    if suffixed:
        m = re.fullmatch(r"(-?\d+\.?\d*)\s*([KMBT][rR]?)?", s)
        if not m:
            return None
        val = float(m.group(1))
        suf = (m.group(2) or "").upper()
        return val * _SUFFIX.get(suf, 1)
    try:
        return float(s)
    except ValueError:
        return None


def _get(url, retries=3, timeout=15):
    """GET page text with retry/backoff on transient errors. Returns None on
    final failure (never raises) so one flaky/slow afx page skips gracefully
    instead of aborting the whole ~280-page pan-African scrape."""
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if r.status_code in (408, 429) or r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        if r.status_code != 200:
            return None
        return r.text
    return None


def list_symbols(exchange, base, cap):
    """Symbols listed on an exchange, from the afx index page links."""
    html = _get(f"{base}/{exchange}/")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    syms = []
    seen = set()
    pat = re.compile(rf"/{exchange}/([a-z0-9]+)\.html$")
    for a in soup.find_all("a", href=True):
        m = pat.search(a["href"])
        if not m:
            continue
        sym = m.group(1).upper()
        if sym not in seen:
            seen.add(sym)
            syms.append(sym)
        if len(syms) >= cap:
            break
    return syms


def fetch_quote(exchange, symbol, base):
    """Scrape one symbol page -> price/volume/change + fundamentals + sector.

    Returns a row dict (values None where not found); None on fetch failure.
    """
    html = _get(f"{base}/{exchange}/{symbol.lower()}.html")
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    txt = _WS.sub(" ", soup.get_text(" "))

    def grab(label, pct=False, suffixed=False):
        m = re.search(re.escape(label) + r"\s+(-?[\d,]+\.?\d*\s*[KMBTr]*)" + (r"\s*%" if pct else ""), txt)
        return _num(m.group(1), suffixed=suffixed) if m else None

    # Latest trade row: "Date Volume Close Change Change% <date> <vol> <close> ...".
    # Grab date/volume/close from the first 3 columns (always present if traded);
    # the change% needs the full 5-column row, which is absent when a stock is flat.
    price = change_pct = volume = trade_date = None
    mt = re.search(r"Change%\s+(\d{4}-\d{2}-\d{2})\s+([\d,]+)\s+([\d,]+\.?\d*)", txt)
    if mt:
        trade_date = mt.group(1)
        volume = _num(mt.group(2))
        price = _num(mt.group(3))
    mc = re.search(
        r"Change%\s+\d{4}-\d{2}-\d{2}\s+[\d,]+\s+[\d,]+\.?\d*\s+[+-]?[\d,]+\.?\d*\s+([+-]?[\d.]+)",
        txt,
    )
    if mc:
        change_pct = _num(mc.group(1))
    if price is None:                       # NSE-style fallback (inline value)
        price = grab("Opening Price")
    if volume is None:                      # inline "Traded Volume 51,809"
        volume = grab("Traded Volume", suffixed=True)

    name = None
    mn = re.search(rf"{symbol}\s*[-–]\s*([^|]+?)\s+(?:\w+ \d+, \d{{4}}|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", txt)
    if mn:
        name = mn.group(1).strip()[:80]

    ms = re.search(r"Sector\s+([A-Za-z &]+?)\s+Industry", txt)
    sector = ms.group(1).strip() if ms else None

    return {
        "exchange": exchange,
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_pct": change_pct,
        "volume": volume,
        "eps": grab("Earnings Per Share"),
        "dps": grab("Dividend Per Share"),
        "pe": grab("Price/Earning Ratio"),
        "yld": grab("Dividend Yield", pct=True),
        "sector": sector,
        "trade_date": trade_date,
    }


def snapshot(acfg, limit_per_exchange=None, log=print):
    """Scrape all configured exchanges and persist one snapshot row per symbol."""
    base = acfg["base_url"]
    pause = acfg.get("pause", 0.25)
    cap = limit_per_exchange or acfg.get("max_per_exchange", 200)
    db = acfg["db_path"]
    init_db(db)

    rows = []
    for ex in acfg["exchanges"]:
        syms = list_symbols(ex, base, cap)
        if not syms:
            log(f"  {ex}: 0 symbols (source unreachable - skipped)")
            continue
        log(f"  {ex}: {len(syms)} symbols")
        for sym in syms:
            try:
                q = fetch_quote(ex, sym, base)
            except requests.RequestException:
                q = None
            if q:
                rows.append(q)
            time.sleep(pause)
    n = save_snapshots(rows, date.today().isoformat(), db)
    log(f"snapshot: saved {n} rows across {len(acfg['exchanges'])} exchange(s).")
    return rows


# ---------------------------------------------------------------- screen ----

def _momentum(history, lookback):
    prices = [h["price"] for h in history if h["price"] is not None]
    if len(prices) <= lookback:
        return None
    old = prices[-(lookback + 1)]
    if not old:
        return None
    return (prices[-1] - old) / old * 100.0


def _avg_volume(history, lookback):
    vols = [h["volume"] for h in history[-lookback:] if h["volume"] is not None]
    return sum(vols) / len(vols) if vols else None


def screen(acfg, run_date=None, log=print):
    """Balanced-blend 'low value / high potential' screen across the universe."""
    run_date = run_date or date.today().isoformat()
    db = acfg["db_path"]
    snap = get_snapshot(run_date, db)
    if not snap:
        return []

    w = acfg["weights"]
    mom_days = acfg.get("momentum_days", 20)
    liq_days = acfg.get("liquidity_days", 10)
    lookback = max(mom_days, liq_days) + 1

    raw_pe, raw_yld, raw_mom, raw_vol, meta = {}, {}, {}, {}, {}
    for r in snap:
        key = (r["exchange"], r["symbol"])
        # Value: only sensible, positive P/E. Exclude losers (<=0) and noise (<1).
        pe = r["pe"]
        raw_pe[key] = pe if (pe is not None and pe >= 1) else None
        raw_yld[key] = r["yld"]
        hist = symbol_history(r["exchange"], r["symbol"], lookback, db)
        raw_mom[key] = _momentum(hist, mom_days)
        raw_vol[key] = _avg_volume(hist, liq_days)
        meta[key] = r

    n_value = _normalize(raw_pe, invert=True)   # lower P/E -> higher score
    n_income = _normalize(raw_yld)
    n_mom = _normalize(raw_mom)
    n_liq = _normalize({k: math.log1p(v) if v else None for k, v in raw_vol.items()})

    results = []
    for key, r in meta.items():
        factors = {"value": n_value[key], "income": n_income[key],
                   "momentum": n_mom[key], "liquidity": n_liq[key]}
        avail = {k: v for k, v in factors.items() if v is not None}
        # This is a VALUE screen: a name needs at least a P/E or a yield to qualify,
        # otherwise data-poor tickers would rank on volume alone. Snapshot is still
        # stored (above) so history accrues; they're just excluded from the ranking.
        if "value" not in avail and "income" not in avail:
            continue
        wsum = sum(w[k] for k in avail)
        score = sum(factors[k] * w[k] for k in avail) / wsum if wsum else None
        results.append({
            "exchange": r["exchange"], "symbol": r["symbol"], "name": r["name"],
            "score": round(score, 2) if score is not None else None,
            "pe": r["pe"], "yld": r["yld"],
            "momentum": round(raw_mom[key], 2) if raw_mom[key] is not None else None,
            "liquidity_flag": 1 if (raw_vol[key] or 0) == 0 else 0,
            "factors_used": sorted(avail),
        })

    results.sort(key=lambda x: (x["score"] is not None, x["score"] or 0), reverse=True)
    for i, r in enumerate(results, 1):
        r["rank"] = i

    k = acfg.get("watchlist_size", 25)
    save_watchlist([{
        "exchange": r["exchange"], "symbol": r["symbol"], "name": r["name"],
        "score": r["score"], "pe": r["pe"], "yld": r["yld"], "momentum": r["momentum"],
        "liquidity_flag": r["liquidity_flag"], "rank": r["rank"],
    } for r in results[:k]], run_date, db)
    log(f"screen: ranked {len(results)}, top {min(k, len(results))} saved.")
    return results


# ------------------------------------------------------------- IPO capture ----

_EX_FROM_URL = re.compile(r"/stock-markets/([a-z]+)/", re.I)


def scan_ipos(acfg, log=print):
    """Reuse the IPO watcher's fetch+filter, but keep ALL African listings
    (not just GSE), tagged by exchange parsed from the source URL."""
    db = acfg["db_path"]
    init_db(db)
    cfg = config.load()
    wcfg = cfg["ipo_watcher"]
    keywords = wcfg["keywords"]
    countries = {k.lower(): v for k, v in acfg["exchanges"].items()}

    raw = []
    for src in wcfg["sources"]:
        raw.extend(ipo_watcher.fetch_source(src, wcfg.get("max_items_per_source", 60)))
    candidates = [it for it in raw if ipo_watcher.keyword_match(it, keywords)]

    seen = ipo_keys(db)
    new = 0
    today = date.today().isoformat()

    def _record(source, title, url, first_seen):
        nonlocal new
        key = (title or "").strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        m = _EX_FROM_URL.search(url or "")
        ex = m.group(1).lower() if m else None
        add_ipo(ex, countries.get(ex), source, title, url, first_seen, db)
        new += 1

    for it in candidates:
        _record(it["source"], it["title"], it.get("url", ""), today)

    # Also ingest anything the GSE watcher already recorded in gse.db, so the two
    # tables never diverge — e.g. items that rotated off a rotating news page
    # before this once-daily scan, or that a transient fetch failure dropped.
    gse_db = cfg.get("storage", {}).get("db_path", "gse.db")
    try:
        with connect(gse_db) as gconn:
            gse_rows = gconn.execute(
                "SELECT source, title, url, first_seen FROM ipo_seen"
            ).fetchall()
        for r in gse_rows:
            _record(r["source"], r["title"], r["url"], r["first_seen"] or today)
    except Exception as e:
        log(f"  ! could not ingest from gse ipo_seen: {type(e).__name__}: {e}")

    log(f"ipos: {new} new pan-African listing(s) recorded.")
    return new


def run(log=print, limit_per_exchange=None):
    """Full pan-African cycle: snapshot -> screen -> IPO scan.

    Returns True if the snapshot captured data. When afx is unreachable the
    snapshot is empty -> returns False so the scheduler treats it as a failed
    run and retries (rather than marking the day done with no data). The IPO
    scan runs regardless: it uses non-afx sources (SEC/GSE/African-Markets) and
    works even during an afx outage.
    """
    acfg = config.load()["africa"]
    rows = snapshot(acfg, limit_per_exchange=limit_per_exchange, log=log)
    if rows:
        screen(acfg, log=log)
    else:
        log("screen: skipped - no snapshot data (afx unreachable).")
    scan_ipos(acfg, log=log)
    return bool(rows)
