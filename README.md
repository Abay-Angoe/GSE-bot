# GSE Bot

A rules-based screener + IPO watcher for the **Ghana Stock Exchange**. It screens
listed equities against tunable rules to produce a ranked watchlist, and watches
SEC/GSE/news sources for new IPOs and listings. Output goes to **Telegram alerts**
and a **read-only Streamlit dashboard**.

> **Not financial advice.** This is a rules-based screener, not a predictor. It flags
> candidates to research, not to buy. Always verify any IPO against the official
> SEC-approved prospectus before any decision.

## Why it persists its own data

The free GSE API ([dev.kwayisi.org/apis/gse](https://dev.kwayisi.org/apis/gse)) has
**no history endpoint** — only current price + last-reported fundamentals. So the bot
saves a daily snapshot to SQLite (`gse.db`); momentum, volume trends, and score-over-time
are computed from that accumulating history. **Run the snapshot daily so history grows.**

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # then fill in Telegram (and optional Anthropic) keys
```

Telegram (optional but recommended):
1. Message **@BotFather** → `/newbot` → copy the token into `TELEGRAM_TOKEN`.
2. Message your new bot, open `https://api.telegram.org/bot<TOKEN>/getUpdates`,
   copy the chat id into `TELEGRAM_CHAT_ID`.
3. `python main.py test-alert` to confirm.

Without Telegram configured, every `--send` path prints a **preview** instead of sending.

## Commands

| Command | What it does |
|---|---|
| `python main.py snapshot` | Pull today's data → save to `gse.db`. |
| `python main.py run-screen [--send]` | Score + rank the universe, write the watchlist; `--send` pushes the digest + event alerts. |
| `python main.py run-ipo [--send]` | Fetch sources → keyword filter → (optional) LLM classify → dedupe → record/alert. |
| `python main.py test-alert` | Send a test Telegram message. |
| `python main.py serve` | Launch the read-only Streamlit dashboard (http://localhost:8501). |
| `python main.py schedule [--now]` | Run the APScheduler loop; `--now` runs both jobs once and exits. |
| `python main.py run-africa [--quick]` | Pan-African value screen + IPO scan into a separate `africa.db`; `--quick` samples 5 symbols/exchange. |
| `python main.py africa-top` | Print the latest pan-African "low value / high potential" watchlist. |
| `python main.py status` | Health check: last run per job, days of history, momentum readiness, recent failures. |

## Configuration

All weights, thresholds, sources, and keywords live in [`config.yaml`](config.yaml) —
tune them without touching code. Screener factor weights are **renormalized per symbol**
over whichever factors are available, so missing data (e.g. the API's null EPS/DPS)
never penalizes a stock.

## Scheduling / deployment

**Option A — built-in scheduler** (self-contained, one long-running process):

```bash
python main.py schedule
```

Runs the full cycle (snapshot → screen → alerts → IPO watch) at the times in the
`schedule:` block of `config.yaml` — by default **09:00 and 15:30 local time on
weekdays**. This is a foreground process: it must stay running and the machine
awake for jobs to fire (it does not survive reboot/sleep on its own).

**Option B — system scheduler** (more robust for always-on boxes):

- *Linux/Pi (cron):*
  ```
  30 15 * * 1-5  cd /path/to/gse-bot && python main.py run-screen --send
  0 */4 * * *    cd /path/to/gse-bot && python main.py run-ipo --send
  ```
- *Windows (Task Scheduler):* create two tasks running
  `python main.py run-screen --send` (daily 15:30) and `python main.py run-ipo --send`
  (every 4h), with "Start in" set to the project folder.

## Layout

```
gse_client.py   API wrapper (kwayisi)
storage.py      SQLite: snapshots, watchlist, ipo_seen
screener.py     factors → normalize → weighted score → rank
ipo_watcher.py  fetch → keyword filter → (LLM) classify → dedupe → alert
alerts.py       Telegram sender + message templates
dashboard.py    read-only Streamlit UI over gse.db
scheduler.py    APScheduler jobs
africa.py       pan-African value screen + IPO tracker (separate africa.db)
main.py         CLI entry point
config.yaml     tunable weights / thresholds / sources
```

## Pan-African screener (optional extension)

`africa.py` is a self-contained module — isolated from the GSE bot, its own
`africa.db` — that scrapes afx.kwayisi.org across several African exchanges
(configurable; default Ghana, Nigeria, Kenya, Uganda, BRVM) and produces a daily
"low value / high potential" watchlist using the same renormalized balanced-blend
scoring. It also keeps a pan-African IPO/listing table (tagged by exchange/country),
including the listings the GSE IPO watcher discards.

It's the heaviest job (hundreds of pages), so it runs **once daily** (`schedule:
africa_time`, default 16:00), sequentially with a polite delay. Notes: fundamentals
coverage varies by exchange (Kenya rich, Nigeria sparse — names without a P/E or
yield are excluded from the value ranking but still snapshotted); factors are
normalized Africa-wide, so "cheap" is measured across the whole universe, not per
local market. A flagging tool, not apples-to-apples valuation.

## Caveats

- Small, illiquid market — momentum and even "current price" are noisy for thin stocks.
  The **liquidity flag** exists to stop the bot treating stale data as a signal.
- The JSON API leaves **EPS/DPS null**, so the snapshot fills them by scraping the same
  provider's web view (afx.kwayisi.org) — see the `fundamentals` block in `config.yaml`
  (set `enabled: false` to disable). Newly-listed names have no published fundamentals yet,
  so they score on liquidity until figures appear.
- When fundamentals are unavailable (afx down), the screener **carries forward the last
  reported EPS/DPS** per symbol. Fundamentals only change on earnings/dividend
  announcements, so last-price ÷ last-reported-EPS is a correct trailing P/E — value/income
  keep scoring through outages instead of going dark.
- Respect the API rate limit (<60 req/s) and each scraped site's terms of service.
- Authoritative IPO sources (SEC Ghana, GSE) are the truth; news outlets are early signals.
- **kwayisi is a single provider** (prices + fundamentals + pan-African). If it's fully
  unreachable, the GSE snapshot falls back to **African-Markets** for prices (degraded:
  prices only, no eps/dps/volume) so an outage doesn't blank the day. A multi-day outage
  still costs those days — there's no history API to backfill (e.g. 07-14/15 were lost
  before the fallback existed).
