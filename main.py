"""GSE Bot CLI entry point.

Commands:
    python main.py snapshot              # pull today's data and persist to gse.db
    python main.py run-screen [--send]   # score + rank, write watchlist; --send pushes to Telegram
    python main.py run-ipo [--send]      # fetch sources, filter/classify IPOs; --send pushes to Telegram
    python main.py test-alert            # send a test Telegram message (checks .env setup)
    python main.py serve                 # launch the read-only Streamlit dashboard
    python main.py schedule [--now]      # run the APScheduler loop; --now runs both jobs once and exits
    python main.py run-africa [--quick]  # pan-African value screen + IPO scan (separate africa.db)
    python main.py africa-top            # print the latest pan-African watchlist
    python main.py status                # health check: last run per job, history, recent failures
"""
import subprocess
import sys
from datetime import date, datetime

# Feed/news titles can contain emoji and non-Latin characters; the default
# Windows console (cp1252) would crash on them. Force UTF-8 output.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv optional; env vars can be set in the shell instead

import africa
import alerts
import gse_client
import ipo_watcher
import screener
import storage


def cmd_snapshot():
    print("Initializing DB...")
    storage.init_db()

    fcfg = screener.config.load().get("fundamentals", {})
    print("Fetching snapshot from GSE API (this takes ~20-40s for ~38 equities)...")
    rows = gse_client.snapshot_all(
        fundamentals=fcfg.get("enabled", True),
        fundamentals_base=fcfg.get("base_url", gse_client.FUNDAMENTALS_BASE),
    )
    if not rows:
        print("No data returned — aborting (nothing written).")
        return 1

    today = date.today().isoformat()
    n = storage.save_snapshot(rows, snapshot_date=today)
    print(f"Saved {n} rows for {today}.")
    print(f"Total rows for today in DB: {storage.snapshot_count(today)}")
    return 0


def cmd_run_screen():
    today = date.today().isoformat()
    if storage.snapshot_count(today) == 0:
        print(f"No snapshot for {today}. Run `python main.py snapshot` first.")
        return 1

    ranked, top = screener.run_and_save(run_date=today)
    if not ranked:
        print("Screener produced no results.")
        return 1

    print(f"Watchlist for {today} (top {len(top)} of {len(ranked)}):\n")
    print(f"{'#':>2}  {'SYM':<7} {'SCORE':>6}  {'PE':>7} {'YIELD':>7} {'MOM%':>7}  LIQ  FACTORS")
    for r in top:
        pe = f"{r['pe']:.2f}" if r["pe"] is not None else "-"
        dy = f"{r['div_yield']:.3f}" if r["div_yield"] is not None else "-"
        mom = f"{r['momentum']:.2f}" if r["momentum"] is not None else "-"
        liq = "FLAG" if r["liquidity_flag"] else "ok"
        sc = f"{r['score']:.2f}" if r["score"] is not None else "-"
        print(f"{r['rank']:>2}  {r['symbol']:<7} {sc:>6}  {pe:>7} {dy:>7} {mom:>7}  {liq:<4} {','.join(r['factors_used'])}")
    print("\nNot financial advice - rules-based flags only. Data as of the snapshot date.")

    if "--send" in sys.argv:
        _send_alerts(today, top)
    return 0


def _send_alerts(today, top):
    cfg = screener.config.load()
    jump = cfg["screener"]["score_jump_alert"]
    summary = alerts.send_watchlist_update(today, top, jump)

    if not summary["sent"]:
        print("\n[--send] Telegram not configured (set TELEGRAM_TOKEN/TELEGRAM_CHAT_ID in .env).")
        print("Preview of messages that would be sent:\n")
        print(summary["digest"])
        if summary["event_msg"]:
            print("\n" + summary["event_msg"])
        return

    print(f"\n[--send] Digest sent. {len(summary['events'])} event(s) vs "
          f"{summary['prev_date'] or 'no prior run'}.")


def cmd_test_alert():
    if not alerts.is_configured():
        print("Telegram not configured. Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in .env")
        print("(copy .env.example to .env and fill in values from BotFather + getUpdates).")
        return 1
    ok = alerts.send_message("*GSE Bot* test message - setup works.")
    print(f"Sent: {ok}")
    return 0 if ok else 1


def cmd_run_ipo():
    send = "--send" in sys.argv
    print("Running IPO watcher...")
    results = ipo_watcher.run(send=send)
    print(f"\nDone. {len(results)} new IPO candidate(s) recorded for today.")
    return 0


def cmd_serve():
    print("Launching Streamlit dashboard (Ctrl+C to stop)...")
    return subprocess.call(
        [sys.executable, "-m", "streamlit", "run", "dashboard.py"]
    )


def cmd_run_africa():
    limit = 5 if "--quick" in sys.argv else None
    if limit:
        print(f"[--quick] limiting to {limit} symbols per exchange (test mode).")
    africa.run(limit_per_exchange=limit)
    return 0


def cmd_africa_top():
    acfg = screener.config.load()["africa"]
    today = date.today().isoformat()
    rows = africa.get_watchlist(today, acfg["db_path"])
    if not rows:
        print(f"No pan-African watchlist for {today}. Run `python main.py run-africa` first.")
        return 1
    print(f"Pan-African 'low value / high potential' - {today} (top {len(rows)}):\n")
    print(f"{'#':>2}  {'EXCH':<5} {'SYM':<11} {'SCORE':>6} {'PE':>8} {'YLD%':>6}  NAME")
    for r in rows:
        pe = f"{r['pe']:.2f}" if r["pe"] is not None else "-"
        yl = f"{r['yld']:.2f}" if r["yld"] is not None else "-"
        sc = f"{r['score']:.2f}" if r["score"] is not None else "-"
        print(f"{r['rank']:>2}  {r['exchange']:<5} {r['symbol']:<11} {sc:>6} {pe:>8} {yl:>6}  {(r['name'] or '')[:32]}")
    print("\nNot financial advice - rules-based flags only. Cross-exchange normalization; verify locally.")
    return 0


def cmd_status():
    import scheduler  # reuse the scheduler's own tz/day/job/state helpers
    cfg = screener.config.load()
    scfg = cfg.get("schedule", {})
    tz = scheduler._resolve_tz(scfg.get("timezone", "local"))
    days = scheduler._parse_days(scfg.get("days", "mon-fri"))
    jobs = scheduler._build_jobs(scfg)
    state = scheduler._load_state()
    now = datetime.now(tz)
    today = now.date().isoformat()
    trading = now.weekday() in days

    print(f"GSE Bot - Status   {now:%Y-%m-%d %A %H:%M} {now.tzname()}\n")

    print("Scheduled jobs (last successful run):")
    for jid, hh, mm, _ in jobs:
        last = state.get(jid, "never")
        if last == today:
            mark = "ran today OK"
        elif not trading:
            mark = "- (not a trading day)"
        elif (now.hour, now.minute) >= (hh, mm):
            mark = "OVERDUE (!) - due earlier today, has not run"
        else:
            mark = f"pending (due {hh:02d}:{mm:02d})"
        print(f"  {jid:10} {hh:02d}:{mm:02d}   last: {last:12}  {mark}")

    print("\nData history (irreplaceable - no history API to backfill):")
    g = storage.distinct_snapshot_dates()
    w = storage.watchlist_dates()
    if g:
        print(f"  GSE:    {len(g):3} snapshot days  ({g[0]} -> {g[-1]})   watchlist: {len(w)} days")
    acfg = cfg["africa"]
    with africa.connect(acfg["db_path"]) as ac:
        adates = [r["date"] for r in ac.execute("SELECT DISTINCT date FROM africa_snapshots ORDER BY date")]
        alast = ac.execute("SELECT COUNT(*) n FROM africa_snapshots WHERE date=?",
                           (adates[-1],)).fetchone()["n"] if adates else 0
        aipos = ac.execute("SELECT COUNT(*) n FROM africa_ipos").fetchone()["n"]
    if adates:
        print(f"  Africa: {len(adates):3} snapshot days  ({adates[0]} -> {adates[-1]})   ~{alast} stocks last run")
    need = cfg["screener"]["momentum_days"]
    required = need + 1  # an N-day return needs N+1 snapshots (today + N ago)
    if len(g) >= required:
        mom = "ACTIVE"
    else:
        n = required - len(g)
        mom = f"~{n} more trading day{'s' if n != 1 else ''} to activate"
    print(f"  Momentum factor: {len(g)}/{required} GSE snapshots -> {mom}")

    with storage.connect() as gc:
        gtot = gc.execute("SELECT COUNT(*) n FROM ipo_seen").fetchone()["n"]
        guns = gc.execute("SELECT COUNT(*) n FROM ipo_seen WHERE sent=0").fetchone()["n"]
    print("\nIPO tracker:")
    print(f"  GSE ipo_seen: {gtot} items ({guns} unsent - Telegram not configured)"
          if guns else f"  GSE ipo_seen: {gtot} items")
    print(f"  Africa ipos:  {aipos} items")

    print("\nScheduler log:")
    try:
        with open(scheduler.LOG_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()
        fails = [ln for ln in lines if "FAILED" in ln][-5:]
        if fails:
            print(f"  recent failures ({len(fails)}):")
            for ln in fails:
                print(f"    {ln[:90]}")
        else:
            print("  no failures logged OK")
        if lines:
            print(f"  last log entry: {lines[-1][:70]}")
    except OSError:
        print("  (no scheduler.log yet)")
    return 0


def cmd_schedule():
    import scheduler
    if "--now" in sys.argv:
        scheduler.run_all_now()
    else:
        scheduler.start()
    return 0


COMMANDS = {
    "snapshot": cmd_snapshot,
    "run-screen": cmd_run_screen,
    "run-ipo": cmd_run_ipo,
    "test-alert": cmd_test_alert,
    "serve": cmd_serve,
    "schedule": cmd_schedule,
    "run-africa": cmd_run_africa,
    "africa-top": cmd_africa_top,
    "status": cmd_status,
}


def main(argv):
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print("Usage: python main.py <command>")
        print("Commands:", ", ".join(COMMANDS))
        return 1
    return COMMANDS[argv[1]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
