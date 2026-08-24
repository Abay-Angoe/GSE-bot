"""Sleep-resilient scheduler that keeps the bot running unattended.

A plain poll loop, deliberately NOT APScheduler: its blocking timer does not
survive an OS suspend (the process stays alive but stops firing after the first
sleep/resume). This loop instead wakes every minute, compares the wall clock to
the configured run times, and runs any job whose time has passed today and hasn't
run yet. That means it self-heals across sleep and CATCHES UP on wake — turn the
laptop on any time after a run time and that day's run still happens.

Run times / timezone come from config.yaml (`schedule:`). Per-job "last run" dates
persist to scheduler_state.json so a restart (e.g. after reboot) doesn't re-run a
job already done today. Each job is wrapped so one failure never kills the loop.

NOTE: nothing can run while the machine is fully asleep; this guarantees catch-up
on the next wake, not execution during suspend.
"""
import json
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import africa
import alerts
import config
import gse_client
import ipo_watcher
import screener
import storage

STATE_FILE = "scheduler_state.json"
POLL_SECONDS = 60
_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


LOG_FILE = "scheduler.log"


def _log(msg):
    line = f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}"
    print(line, flush=True)
    try:                                  # also persist so runs are auditable
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def job_snapshot_and_screen():
    """Snapshot, rescreen, push alerts. Returns True on success, False otherwise."""
    try:
        storage.init_db()
        today = date.today().isoformat()

        fcfg = config.load().get("fundamentals", {})
        rows = gse_client.snapshot_all(
            fundamentals=fcfg.get("enabled", True),
            fundamentals_base=fcfg.get("base_url", gse_client.FUNDAMENTALS_BASE),
        )
        if not rows:
            _log("snapshot: no data returned (will retry next poll).")
            return False
        n = storage.save_snapshot(rows, snapshot_date=today)
        _log(f"snapshot: saved {n} rows for {today}.")

        ranked, top = screener.run_and_save(run_date=today)
        if not top:
            _log("screen: no results.")
            return False
        _log(f"screen: ranked {len(ranked)}, top {len(top)} written to watchlist.")

        cfg = config.load()
        jump = cfg["screener"]["score_jump_alert"]
        summary = alerts.send_watchlist_update(today, top, jump)
        if summary["sent"]:
            _log(f"alerts: digest sent, {len(summary['events'])} event(s).")
        else:
            _log("alerts: Telegram not configured, nothing sent.")
        return True
    except Exception as e:
        _log(f"job_snapshot_and_screen FAILED (will retry next poll): {type(e).__name__}: {e}")
        return False


def job_ipo_watch():
    """Fetch sources, filter/classify, dedupe, and alert. Returns success bool."""
    try:
        results = ipo_watcher.run(send=True)
        _log(f"ipo: {len(results)} new candidate(s).")
        return True
    except Exception as e:
        _log(f"job_ipo_watch FAILED: {type(e).__name__}: {e}")
        return False


def job_full_run():
    """Full cycle. Success is gated on the snapshot/screen (the history-critical
    part); a failed IPO watch is logged but doesn't hold back the day's data."""
    ok = job_snapshot_and_screen()
    job_ipo_watch()
    return ok


def job_africa():
    """Pan-African value screen + IPO scan (heavy; once daily). Returns success bool.

    Success is gated on the snapshot capturing data — an afx outage yields no rows
    and returns False so the run isn't marked done and retries on the next poll."""
    try:
        ok = africa.run(log=_log)
        if not ok:
            _log("job_africa: no data captured (afx unreachable) - will retry next poll.")
        return ok
    except Exception as e:
        _log(f"job_africa FAILED (will retry next poll): {type(e).__name__}: {e}")
        return False


def run_all_now():
    """Run the full cycle once immediately (for testing / a manual catch-up)."""
    _log("Running full cycle once now...")
    job_full_run()
    _log("Done.")


def _resolve_tz(name):
    if not name or str(name).lower() == "local":
        return datetime.now().astimezone().tzinfo
    return ZoneInfo(name)


def _parse_days(spec):
    """'mon-fri' / 'mon-sun' / 'mon,wed,fri' -> set of weekday ints (mon=0)."""
    spec = str(spec).strip().lower()
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            ia, ib = _WEEKDAYS.index(a), _WEEKDAYS.index(b)
            out.update(range(ia, ib + 1) if ia <= ib else list(range(ia, 7)) + list(range(0, ib + 1)))
        elif part in _WEEKDAYS:
            out.add(_WEEKDAYS.index(part))
    return out or set(range(5))  # default mon-fri


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def _build_jobs(scfg):
    """List of (job_id, hour, minute, callable) from config."""
    jobs = []
    for t in scfg.get("run_times", ["09:00", "15:30"]):
        hh, mm = (int(x) for x in str(t).split(":"))
        jobs.append((f"run_{hh:02d}{mm:02d}", hh, mm, job_full_run))
    africa_time = scfg.get("africa_time")
    if africa_time:
        ah, am = (int(x) for x in str(africa_time).split(":"))
        jobs.append(("africa", ah, am, job_africa))
    return sorted(jobs, key=lambda j: (j[1], j[2]))


def start():
    """Run the poll loop until interrupted. Catches up missed runs on wake."""
    scfg = config.load().get("schedule", {})
    tz = _resolve_tz(scfg.get("timezone", "local"))
    days = _parse_days(scfg.get("days", "mon-fri"))
    jobs = _build_jobs(scfg)
    state = _load_state()

    _log(f"Scheduler (poll loop) started. Timezone: {tz}. Jobs:")
    for jid, hh, mm, _ in jobs:
        _log(f"  - {jid}: {hh:02d}:{mm:02d} on days {sorted(days)} (last run: {state.get(jid, 'never')})")
    _log(f"Polling every {POLL_SECONDS}s; missed runs catch up on wake. Stop with Ctrl+C / kill.")

    try:
        while True:
            now = datetime.now(tz)
            today = now.date().isoformat()
            if now.weekday() in days:
                for jid, hh, mm, fn in jobs:
                    due = (now.hour, now.minute) >= (hh, mm)
                    if due and state.get(jid) != today:
                        _log(f"Running '{jid}' (scheduled {hh:02d}:{mm:02d}, now {now:%H:%M}).")
                        # Only mark done on success, so a transient failure (e.g. a
                        # network blip mid-snapshot) retries on the next poll.
                        if fn():
                            state[jid] = today
                            _save_state(state)
            time.sleep(POLL_SECONDS)
    except (KeyboardInterrupt, SystemExit):
        _log("Scheduler stopped.")
