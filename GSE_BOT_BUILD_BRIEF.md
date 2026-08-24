# GSE Bot — Build Brief

A bot for the **Ghana Stock Exchange (GSE)** that (a) screens listed stocks against
tunable rules to produce a ranked watchlist, and (b) watches for new IPOs / listing
announcements. Output goes to **Telegram alerts** and a **simple dashboard**.

> **Framing / disclaimer.** This is a *rules-based screener*, not a predictor. It flags
> candidates for the user to research; it does not give investment advice. IPO details it
> surfaces must always be verified against the official SEC-approved prospectus before any
> decision. Keep this disclaimer visible in the dashboard and in alerts.

---

## 1. Why the architecture looks the way it does

The GSE is **small** (~38 listed equities) and **end-of-day**, not real-time tick. Two
consequences drive every design choice:

1. **Tiny universe → screening is cheap.** A full scan is dozens of HTTP calls, runnable
   once daily. No need for streaming or a heavy data pipeline.
2. **No historical-data endpoint.** The free API returns only *current* price and *last
   reported* fundamentals. Therefore the bot **must persist its own daily snapshots** to a
   local DB so that momentum, volume trends, and "score over time" can be computed. This is
   the single most important requirement — build the snapshot store early so history starts
   accumulating from day one.

---

## 2. Data sources

### Primary (free, no key) — `https://dev.kwayisi.org/apis/gse`

Verified endpoints and response shapes:

| Endpoint | Returns | Fields |
|---|---|---|
| `GET /equities` | array, one per listed equity | `name` (ticker), `price` |
| `GET /equities/{symbol}` | full fundamentals for one equity | `capital` (market cap, GHS), `eps`, `dps`, `price`, `shares`, `company{ sector, industry, name, address, directors[], telephone, email, website }` |
| `GET /live` | array, real-time-ish per equity | `name`, `price`, `change`, `volume` |
| `GET /live/{symbol}` | one live object | same as above |

Notes / gotchas:
- **Rate limit:** stay well under 60 requests/second or you get HTTP 429. For ~38 equities,
  fetch sequentially with a small delay (e.g. 0.2s) and exponential backoff on 429.
- **No auth, no history.** Confirmed by the maintainer. Build our own history (see §3).
- **Thin trading:** many equities barely trade, so `price`/`change`/`volume` can be stale
  for days. Always compute and surface a **liquidity flag** so the screener doesn't treat a
  stale price as a real signal.
- Metrics you can derive directly: `pe = price / eps` (guard eps>0), `dividend_yield =
  dps / price`, `market_cap = capital`.

### Secondary (for the IPO watcher — scrape/RSS, respect each site's ToS)
- **SEC Ghana** (sec.gov.gh) — prospectus approvals / public notices (authoritative).
- **GSE** (gse.com.gh) — official listing announcements / circulars.
- News: MyJoyOnline (business), GhanaWeb (business), B&FT (thebftonline.com),
  African Markets GSE page (african-markets.com/en/stock-markets/gse).

### Optional paid (only if you outgrow free)
- EODHD or Intrinio for validated EOD history (Intrinio has GSE history back to 2007).

---

## 3. Components / project layout

```
gse-bot/
  config.yaml          # weights, thresholds, schedule, source URLs, watchlist size
  gse_client.py        # wrapper around the kwayisi API (see §7 reference)
  storage.py           # SQLite: daily price/fundamental snapshots + watchlist + seen-IPOs
  screener.py          # compute metrics, normalize, score, rank
  ipo_watcher.py       # fetch sources, filter, classify, dedupe
  alerts.py            # Telegram sender + message formatting
  dashboard.py         # Streamlit (fast) OR FastAPI + one HTML page
  scheduler.py         # APScheduler jobs (or use system cron)
  main.py              # CLI entry: `run-screen`, `run-ipo`, `serve`, `snapshot`
  .env                 # TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY (optional)
  requirements.txt
  gse.db               # created at runtime
```

**Storage schema (SQLite):**
- `snapshots(date, symbol, price, change, volume, eps, dps, capital, shares, sector)`
  — one row per symbol per day; primary key `(date, symbol)`.
- `watchlist(date, symbol, score, pe, div_yield, momentum, liquidity_flag, rank)`
  — the screener's daily output, kept for history.
- `ipo_seen(id, source, title, url, company, first_seen, sent)` — dedupe for the watcher.

---

## 4. Screener logic (transparent + tunable)

Run once per trading day after close. Steps:

1. **Snapshot:** pull `/equities`, then `/equities/{symbol}` for each ticker and `/live`
   for volume/change. Write a row per symbol to `snapshots`.
2. **Compute factors** per symbol:
   - `value` — lower P/E scores higher (rank within sector where possible; fall back to
     market-wide). Skip if `eps <= 0`.
   - `income` — dividend yield (`dps/price`).
   - `momentum` — N-day price return from `snapshots` (e.g. 20 trading days). Null until
     enough history exists — handle gracefully.
   - `liquidity` — average traded `volume` over last N days; also set a boolean
     `liquidity_flag` when volume is ~0 for several days.
3. **Normalize** each factor to 0–100 across the universe, then **weighted sum** →
   `composite_score`. Weights live in `config.yaml` so the user tunes them without code
   changes. Sensible defaults: value 0.3, income 0.2, momentum 0.3, liquidity 0.2.
4. **Rank**, take top K (config, e.g. 10), write to `watchlist`.
5. **Alert** on: new entrant to the top K, or a symbol whose score jumped > threshold.
   Always include the liquidity flag in the message.

Keep scoring explainable — every alert should show *why* (the factor values), not just a
number. Do **not** add black-box ML; the universe is too small and the data too sparse for
it to mean anything.

---

## 5. IPO watcher

Run a few times per day:

1. **Fetch** each source (RSS where available, else fetch + parse HTML).
2. **Keyword pre-filter** on title/body: "IPO", "initial public offering", "offer for
   subscription", "prospectus", "to list" / "lists on", "cross-listing", "GAX",
   "right issue". Cheap first pass.
3. **Classify + extract (optional but recommended):** pass each candidate to an LLM (Claude
   Haiku via the Anthropic API is a good cost/latency fit) with a strict JSON-only prompt to
   return `{is_ipo: bool, company, ticker, offer_price, open_date, close_date, listing_date,
   confidence}`. Parse defensively; ignore low confidence.
4. **Dedupe** against `ipo_seen` (by normalized title + company). 
5. **Alert** only genuinely new items. Include source link so the user can open the
   official notice.

The SEC/GSE sources are authoritative; treat news outlets as early signals to be confirmed.

---

## 6. Output channels

### Telegram (alerts)
Setup the user does once:
1. Message **@BotFather** → `/newbot` → copy the **bot token**.
2. Start a chat with the new bot, send any message, then hit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` to read your **chat id** (or use
   @userinfobot).
3. Put `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`.
Send via Bot API `sendMessage` (Markdown formatting). Keep two message templates: a daily
watchlist digest and a one-off IPO alert.

### Dashboard
Two options — pick by how "always-on" it needs to be:
- **Streamlit** (fastest to build): one script reads `gse.db` and renders the ranked
  watchlist table + an IPO feed + a factor-score chart. Great for a single user.
- **FastAPI + one Jinja/HTML page** if you want it running as a small always-on service.
Either way the dashboard is **read-only over the SQLite DB** — the jobs write, the
dashboard reads. Show the disclaimer banner.

---

## 7. Reference GSE client (verified — gives Claude Code a known-good starting point)

```python
import time
import requests

BASE = "https://dev.kwayisi.org/apis/gse"

def _get(path, retries=3):
    for attempt in range(retries):
        r = requests.get(f"{BASE}{path}", timeout=15)
        if r.status_code == 429:
            time.sleep(2 ** attempt)      # backoff on rate limit
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"rate-limited on {path}")

def list_tickers():
    """['MTNGH', 'GCB', 'EGH', ...] — names ARE the ticker symbols."""
    return [e["name"] for e in _get("/equities")]

def get_equity(symbol):
    """Full fundamentals: capital (mkt cap GHS), eps, dps, price, shares, company{...}."""
    return _get(f"/equities/{symbol}")

def get_live():
    """[{'name','price','change','volume'}, ...] — current-session figures."""
    return _get("/live")

def snapshot_all(pause=0.2):
    """One row per symbol; merges fundamentals with live volume/change."""
    live = {x["name"]: x for x in (get_live() or [])}
    out = []
    for sym in list_tickers():
        eq = get_equity(sym)
        if not eq:
            continue
        lv = live.get(sym, {})
        out.append({
            "symbol": sym,
            "price": eq.get("price"),
            "eps": eq.get("eps"),
            "dps": eq.get("dps"),
            "capital": eq.get("capital"),
            "shares": eq.get("shares"),
            "sector": (eq.get("company") or {}).get("sector"),
            "change": lv.get("change"),
            "volume": lv.get("volume"),
        })
        time.sleep(pause)
    return out
```

---

## 8. Tech stack

Python 3.11+, `requests`, `sqlite3` (stdlib), `APScheduler` (or cron), `python-dotenv`,
`feedparser` + `beautifulsoup4` (IPO scraping), `streamlit` *or* `fastapi`+`uvicorn`+`jinja2`,
and optionally the `anthropic` SDK for IPO classification.

**Scheduling:** GSE trades weekdays ~10:00–15:00 GMT (Ghana is GMT year-round). Run the
snapshot+screen job at ~15:30 GMT on weekdays; run the IPO watcher every ~4 hours.

---

## 9. Suggested build order for Claude Code

1. Scaffold repo + `gse_client.py`; smoke-test all four endpoints live.
2. `storage.py` + `snapshot` job → confirm rows land in SQLite. (History starts now.)
3. `screener.py` with config-driven weights; produce a ranked watchlist (momentum will be
   null until ~20 days of snapshots exist — handle that path).
4. `alerts.py` + Telegram; send the daily digest.
5. `ipo_watcher.py`: sources → keyword filter → (optional) LLM classify → dedupe → alert.
6. `dashboard.py` (Streamlit first).
7. `scheduler.py`; deploy on a cheap always-on host (small VPS, or a Raspberry Pi) so cron
   keeps snapshots accumulating.

---

## 10. Caveats to keep in mind while building

- **Not financial advice**; rules-based flags only. Verify IPOs against the official
  prospectus/SEC notice.
- **Small, illiquid market** → momentum and even "current price" are noisy for thin stocks.
  The liquidity flag exists to stop the bot from treating stale data as signal.
- **Respect rate limits** (<60 req/s) and each scraped site's terms of service.
- **Data can be stale**; show the snapshot date on everything.
