"""Wrapper around the kwayisi GSE API.

Free, no-auth, end-of-day-ish data for the Ghana Stock Exchange.
No history endpoint exists, so the bot persists its own snapshots (see storage.py).
"""
import re
import time
import requests
from bs4 import BeautifulSoup

BASE = "https://dev.kwayisi.org/apis/gse"

# The JSON API leaves eps/dps null, but the same provider's human web view
# (afx.kwayisi.org) publishes them. Scrape that as a fundamentals fallback so the
# screener's value (P/E) and income (yield) factors can activate. Same source as
# our prices, so the figures reconcile (price = P/E x EPS = DPS / yield).
FUNDAMENTALS_BASE = "https://afx.kwayisi.org/gse"
_FUND_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GSE-Bot/1.0)"}


def _get(path, retries=4):
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(f"{BASE}{path}", timeout=15)
        except requests.RequestException as e:
            # Transient network/timeout errors are common; back off and retry
            # rather than letting one flaky connection abort a whole snapshot.
            last_err = e
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429:
            time.sleep(2 ** attempt)  # backoff on rate limit
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"giving up on {path} after {retries} tries: {last_err or 'rate-limited'}")


def list_tickers():
    """['MTNGH', 'GCB', 'EGH', ...] — names ARE the ticker symbols."""
    return [e["name"] for e in _get("/equities")]


def get_equity(symbol):
    """Full fundamentals: capital (mkt cap GHS), eps, dps, price, shares, company{...}."""
    return _get(f"/equities/{symbol}")


def get_live():
    """[{'name','price','change','volume'}, ...] — current-session figures."""
    return _get("/live")


def get_fundamentals(symbol, base=FUNDAMENTALS_BASE, timeout=15):
    """Scrape eps/dps (plus pe/yield) for one symbol from the afx web view.

    Returns {'eps','dps','pe','yld'} with None for any value not found. Never
    raises — a scrape failure just yields all-None so snapshots still proceed.
    """
    out = {"eps": None, "dps": None, "pe": None, "yld": None}
    try:
        r = requests.get(f"{base}/{symbol}", headers=_FUND_HEADERS, timeout=timeout)
        if r.status_code != 200:
            return out
        text = BeautifulSoup(r.text, "html.parser").get_text(" ")
    except requests.RequestException:
        return out

    def grab(label, pct=False):
        m = re.search(re.escape(label) + r"\s+(-?[\d,]+\.?\d*)" + (r"\s*%" if pct else ""), text)
        return float(m.group(1).replace(",", "")) if m else None

    out["eps"] = grab("Earnings Per Share")
    out["pe"] = grab("Price/Earning Ratio")
    out["dps"] = grab("Dividend Per Share")
    out["yld"] = grab("Dividend Yield", pct=True)
    return out


# --- Fallback source: African-Markets (used only when kwayisi is down) ---------
# kwayisi is a single provider behind prices AND fundamentals; a multi-day outage
# (as on 2026-07-14/15) blanks the whole day. African-Markets stays up independently
# and lists the full GSE universe with the same ticker codes, so we can capture
# prices from it as a degraded fallback (no eps/dps/volume — those need kwayisi).
FALLBACK_URL = "https://www.african-markets.com/en/stock-markets/gse/listed-companies"
_FALLBACK_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
_CODE_RE = re.compile(r"code=([A-Za-z0-9]+)")


def _num(s):
    if s is None:
        return None
    s = s.strip().replace(",", "").replace("%", "").replace("+", "")
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def get_gse_prices_fallback(url=FALLBACK_URL, timeout=20):
    """Scrape the full GSE universe (ticker, price, 1D change, sector) from
    African-Markets. Prices only — eps/dps/volume need kwayisi and stay None."""
    r = requests.get(url, headers=_FALLBACK_HEADERS, timeout=timeout)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")  # first table is the listed-companies grid
    out = []
    if not table:
        return out
    for tr in table.find_all("tr"):
        a = tr.find("a", href=_CODE_RE)
        if not a:
            continue
        m = _CODE_RE.search(a["href"])
        cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        if not m or len(cells) < 4:
            continue
        # columns: Company, Sector, Price, 1D, YTD, M.Cap, Date
        out.append({
            "symbol": m.group(1).upper(),
            "price": _num(cells[2]),
            "change": _num(cells[3]),
            "volume": None, "eps": None, "dps": None, "capital": None, "shares": None,
            "sector": cells[1] or None,
        })
    return out


def snapshot_all(pause=0.2, fundamentals=True, fundamentals_base=FUNDAMENTALS_BASE, fallback=True):
    """One row per symbol; merges fundamentals with live volume/change.

    Tries kwayisi first (full data). If kwayisi is fully unreachable and
    `fallback` is True, captures prices from African-Markets instead so an outage
    doesn't blank the day (degraded: prices only, no eps/dps/volume).
    """
    try:
        return _snapshot_kwayisi(pause, fundamentals, fundamentals_base)
    except Exception as e:
        if not fallback:
            raise
        print(f"  kwayisi unavailable ({type(e).__name__}); trying African-Markets fallback...")
        rows = get_gse_prices_fallback()
        print(f"  fallback: {len(rows)} equities from African-Markets (prices only, no eps/dps/volume).")
        return rows


def _snapshot_kwayisi(pause, fundamentals, fundamentals_base):
    live = {x["name"]: x for x in (get_live() or [])}
    out, failed = [], []
    for sym in list_tickers():
        try:
            eq = get_equity(sym)
        except Exception as e:  # one bad symbol must not abort the whole snapshot
            failed.append(sym)
            print(f"  ! skipped {sym}: {type(e).__name__}: {e}")
            continue
        if not eq:
            continue
        lv = live.get(sym, {})
        eps, dps = eq.get("eps"), eq.get("dps")
        if fundamentals and (eps is None or dps is None):
            f = get_fundamentals(sym, base=fundamentals_base)
            if eps is None:
                eps = f["eps"]
            if dps is None:
                dps = f["dps"]
        out.append({
            "symbol": sym,
            "price": eq.get("price"),
            "eps": eps,
            "dps": dps,
            "capital": eq.get("capital"),
            "shares": eq.get("shares"),
            "sector": (eq.get("company") or {}).get("sector"),
            "change": lv.get("change"),
            "volume": lv.get("volume"),
        })
        time.sleep(pause)
    if failed:
        print(f"  ({len(failed)} symbol(s) skipped after retries: {', '.join(failed)})")
    return out
