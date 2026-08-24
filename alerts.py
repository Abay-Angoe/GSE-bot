"""Telegram alerts: daily watchlist digest + event alerts (new entrants, score jumps).

Sending uses the Telegram Bot API `sendMessage` over stdlib urllib (no extra dep).
Credentials come from the environment (.env): TELEGRAM_TOKEN, TELEGRAM_CHAT_ID.

Every message carries the not-financial-advice disclaimer and the snapshot date,
and surfaces the liquidity flag so a stale price is never mistaken for a signal.
"""
import json
import os
import urllib.parse
import urllib.request

import storage

DISCLAIMER = "_Not financial advice - rules-based flags only. Verify before acting._"
API = "https://api.telegram.org/bot{token}/sendMessage"


def _creds():
    return os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")


def is_configured():
    token, chat = _creds()
    return bool(token and chat)


def send_message(text, parse_mode="Markdown"):
    """Send one message. Returns True on success. Raises only on configuration error."""
    token, chat = _creds()
    if not (token and chat):
        raise RuntimeError(
            "Telegram not configured: set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in .env"
        )
    data = urllib.parse.urlencode({
        "chat_id": chat,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(API.format(token=token), data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode())
            return bool(payload.get("ok"))
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"Telegram send failed (HTTP {e.code}): {body}")
        return False
    except urllib.error.URLError as e:
        print(f"Telegram send failed (network): {e.reason}")
        return False


def _liq(flag):
    return " *[thin/stale]*" if flag else ""


def _fmt(value, spec, dash="-"):
    return format(value, spec) if value is not None else dash


def format_digest(top, run_date):
    """Daily watchlist digest as Telegram Markdown."""
    lines = [f"*GSE Watchlist - {run_date}*", ""]
    for r in top:
        score = _fmt(r["score"], ".1f")
        bits = []
        if r.get("pe") is not None:
            bits.append(f"P/E {r['pe']:.1f}")
        if r.get("div_yield") is not None:
            bits.append(f"yld {r['div_yield'] * 100:.1f}%")
        if r.get("momentum") is not None:
            bits.append(f"mom {r['momentum']:+.1f}%")
        detail = ("  (" + ", ".join(bits) + ")") if bits else ""
        lines.append(f"{r['rank']}. *{r['symbol']}* - {score}{detail}{_liq(r['liquidity_flag'])}")
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def format_ipo_alert(item):
    """One-off IPO alert as Telegram Markdown. `item` is a watcher result dict."""
    title = item.get("title") or "(untitled)"
    lines = [f"*Possible IPO / listing*", "", title]

    facts = []
    for label, key in (("Company", "company"), ("Ticker", "ticker"),
                       ("Offer price", "offer_price"), ("Opens", "open_date"),
                       ("Closes", "close_date"), ("Lists", "listing_date")):
        val = item.get(key)
        if val:
            facts.append(f"{label}: {val}")
    if facts:
        lines += ["", "\n".join(facts)]

    conf = item.get("confidence")
    if conf is not None:
        lines.append(f"\nConfidence: {conf:.0%}")
    if item.get("url"):
        lines.append(f"Source ({item.get('source', 'link')}): {item['url']}")
    lines += [
        "",
        "_Verify against the official SEC-approved prospectus before any decision._",
    ]
    return "\n".join(lines)


def send_watchlist_update(run_date, top, jump_threshold):
    """Compute events vs the previous watchlist and send digest + event alerts.

    First run with no prior watchlist establishes a baseline (digest only) so we
    don't fire a spurious 'new entrant' for every symbol. Returns a summary dict;
    if Telegram isn't configured, `sent` is False and the formatted messages are
    returned for preview instead of being sent.
    """
    prev_date = storage.previous_watchlist_date(run_date)
    events = compute_events(top, storage.get_watchlist(prev_date), jump_threshold) if prev_date else []
    digest = format_digest(top, run_date)
    event_msg = format_events(events, run_date)

    summary = {
        "prev_date": prev_date, "events": events,
        "digest": digest, "event_msg": event_msg, "sent": False,
    }
    if is_configured():
        send_message(digest)
        if event_msg:
            send_message(event_msg)
        summary["sent"] = True
    return summary


def compute_events(today_rows, prev_rows, jump_threshold):
    """Detect new top-K entrants and score jumps vs the previous watchlist.

    Returns a list of {type, symbol, ...} event dicts.
    """
    prev_by_sym = {r["symbol"]: r for r in prev_rows}
    prev_syms = set(prev_by_sym)
    events = []
    for r in today_rows:
        sym = r["symbol"]
        if sym not in prev_syms:
            events.append({"type": "new_entrant", "symbol": sym, "row": r})
        else:
            old = prev_by_sym[sym].get("score")
            new = r.get("score")
            if old is not None and new is not None and (new - old) >= jump_threshold:
                events.append({
                    "type": "score_jump", "symbol": sym, "row": r,
                    "delta": new - old, "old_score": old,
                })
    return events


def format_events(events, run_date):
    """Event alerts as Telegram Markdown. Returns None if there are no events."""
    if not events:
        return None
    lines = [f"*GSE Alerts - {run_date}*", ""]
    for e in events:
        r = e["row"]
        sc = _fmt(r["score"], ".1f")
        if e["type"] == "new_entrant":
            lines.append(f"New in top list: *{r['symbol']}* (rank {r['rank']}, score {sc}){_liq(r['liquidity_flag'])}")
        else:
            lines.append(
                f"Score jump: *{r['symbol']}* {e['old_score']:.1f} -> {sc} "
                f"(+{e['delta']:.1f}, rank {r['rank']}){_liq(r['liquidity_flag'])}"
            )
    lines += ["", DISCLAIMER]
    return "\n".join(lines)
