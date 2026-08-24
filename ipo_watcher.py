"""IPO / new-listing watcher.

Pipeline: fetch sources (RSS or scraped HTML) -> cheap keyword pre-filter ->
optional Claude classification/extraction -> dedupe against ipo_seen -> alert.

Authoritative sources (SEC Ghana, GSE) are the truth; news outlets are early
signals to be confirmed. Every alert points to the source so the user can open
the official notice, and reminds them to verify against the prospectus.
"""
import json
import os
import re
import time
import urllib.parse
from datetime import date

import feedparser
import requests
from bs4 import BeautifulSoup

import alerts
import config
import storage

USER_AGENT = "Mozilla/5.0 (compatible; GSE-Bot/1.0; +https://example.local)"
_WS = re.compile(r"\s+")


def _norm(s):
    return _WS.sub(" ", (s or "")).strip().lower()


def dedupe_key(title, company=""):
    return f"{_norm(title)}|{_norm(company)}"


# ---------------------------------------------------------------- fetching ----

def _fetch(url, retries=3, timeout=20):
    """GET raw bytes with retry/backoff on transient errors (timeouts, 408/429/5xx).

    Used by both RSS and HTML fetching so a flaky source (e.g. african-markets
    returning HTTP 408) doesn't silently drop the whole source on the first blip.
    Raises the last error if all attempts fail.
    """
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
            if r.status_code in (408, 429) or r.status_code >= 500:
                last_err = RuntimeError(f"HTTP {r.status_code}")
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.content
        except requests.RequestException as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise last_err or RuntimeError(f"failed to fetch {url}")


def fetch_rss(source, cap):
    feed = feedparser.parse(_fetch(source["url"]))
    items = []
    for e in feed.entries[:cap]:
        items.append({
            "source": source["name"],
            "authoritative": source.get("authoritative", False),
            "title": (e.get("title") or "").strip(),
            "url": e.get("link") or source["url"],
            "summary": _WS.sub(" ", (e.get("summary") or ""))[:1000],
        })
    return items


def fetch_html(source, cap):
    """Crude but source-agnostic: scrape anchor links and treat each as a candidate.

    The keyword filter downstream does the real selection, so over-collecting here
    is fine (and capped per source).
    """
    html = _fetch(source["url"])
    soup = BeautifulSoup(html, "html.parser")
    base = source["url"]
    items, seen_local = [], set()
    for a in soup.find_all("a", href=True):
        text = _WS.sub(" ", a.get_text(" ")).strip()
        if len(text) < 15:  # skip nav/icon links
            continue
        href = urllib.parse.urljoin(base, a["href"])
        if href in seen_local:
            continue
        seen_local.add(href)
        items.append({
            "source": source["name"],
            "authoritative": source.get("authoritative", False),
            "title": text,
            "url": href,
            "summary": "",
        })
        if len(items) >= cap:
            break
    return items


def fetch_source(source, cap):
    try:
        if source["type"] == "rss":
            return fetch_rss(source, cap)
        return fetch_html(source, cap)
    except Exception as e:  # one bad source must not kill the run
        print(f"  ! source '{source['name']}' failed: {type(e).__name__}: {e}")
        return []


# ------------------------------------------------------- filter / classify ----

def keyword_match(item, keywords):
    hay = (item["title"] + " " + item["summary"]).lower()
    return any(k.lower() in hay for k in keywords)


_LLM_PROMPT = (
    "You classify Ghana Stock Exchange news snippets. Decide if the text announces "
    "a NEW initial public offering, share offer for subscription, or new listing "
    "(IPO/GAX listing/cross-listing/rights issue). Reply with ONLY a JSON object, "
    "no prose, with keys: is_ipo (bool), company (string|null), ticker (string|null), "
    "offer_price (string|null), open_date (string|null), close_date (string|null), "
    "listing_date (string|null), confidence (0..1 float). If unsure, is_ipo=false.\n\n"
    "TEXT:\n"
)


GEMINI_KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


def _provider(cfg):
    return cfg["ipo_watcher"].get("llm_provider", "gemini").lower()


def llm_key_present(cfg):
    """True if the configured provider's API key is available."""
    if _provider(cfg) == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    return any(os.environ.get(k) for k in GEMINI_KEYS)


def _llm_model(cfg, provider):
    models = cfg["ipo_watcher"].get("llm_models", {})
    return models.get(provider)


def _parse_json(text):
    m = re.search(r"\{.*\}", text or "", re.DOTALL)  # parse defensively
    return json.loads(m.group(0)) if m else None


def _classify_gemini(prompt, cfg):
    key = next((os.environ[k] for k in GEMINI_KEYS if os.environ.get(k)), None)
    model = _llm_model(cfg, "gemini")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
    }
    r = requests.post(url, params={"key": key}, json=body, timeout=20)
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return _parse_json("".join(p.get("text", "") for p in parts))


def _classify_anthropic(prompt, cfg):
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=_llm_model(cfg, "anthropic"),
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return _parse_json(text)


def classify_llm(item, cfg):
    """Return the extracted dict via the configured LLM, or None on failure."""
    if not llm_key_present(cfg):
        return None
    prompt = _LLM_PROMPT + f"{item['title']}\n{item['summary']}"
    provider = _provider(cfg)
    try:
        if provider == "anthropic":
            return _classify_anthropic(prompt, cfg)
        return _classify_gemini(prompt, cfg)
    except Exception as e:
        print(f"  ! LLM classify ({provider}) failed: {type(e).__name__}: {e}")
        return None


# ----------------------------------------------------------------- runner ----

def run(cfg=None, send=False, run_date=None):
    """Fetch -> filter -> (classify) -> dedupe -> persist -> alert.

    Returns the list of newly-recorded IPO candidate dicts.
    """
    cfg = cfg or config.load()
    wcfg = cfg["ipo_watcher"]
    run_date = run_date or date.today().isoformat()
    storage.init_db()

    cap = wcfg.get("max_items_per_source", 60)
    keywords = wcfg["keywords"]
    use_llm = wcfg.get("use_llm", False) and llm_key_present(cfg)
    min_conf = wcfg.get("llm_min_confidence", 0.5)

    # 1. Fetch
    raw = []
    for src in wcfg["sources"]:
        got = fetch_source(src, cap)
        print(f"  {src['name']}: {len(got)} item(s)")
        raw.extend(got)

    # 2. Keyword pre-filter
    candidates = [it for it in raw if keyword_match(it, keywords)]
    print(f"Keyword-matched candidates: {len(candidates)} of {len(raw)}")

    # 3. Dedupe vs already-seen (and within this run)
    seen = storage.ipo_seen_keys()
    fresh = []
    for it in candidates:
        key = dedupe_key(it["title"])
        if key in seen:
            continue
        seen.add(key)
        fresh.append(it)
    print(f"New (not previously seen): {len(fresh)}")

    # 4. Optional LLM classify/extract + confidence gate
    results = []
    for it in fresh:
        extracted = classify_llm(it, cfg) if use_llm else None
        if extracted is not None:
            if not extracted.get("is_ipo"):
                continue
            if (extracted.get("confidence") or 0) < min_conf:
                continue
            it = {**it, **extracted}
        results.append(it)
    if use_llm:
        print(f"LLM-confirmed IPOs ({_provider(cfg)}): {len(results)}")
    else:
        print("LLM disabled (no key or use_llm=false) - using keyword matches as-is.")

    # 5. Persist + alert
    for it in results:
        storage.add_ipo(
            source=it.get("source"),
            title=it.get("title"),
            url=it.get("url"),
            company=it.get("company"),
            first_seen=run_date,
            sent=1 if send and alerts.is_configured() else 0,
        )
        if send and alerts.is_configured():
            alerts.send_message(alerts.format_ipo_alert(it))

    if results and not (send and alerts.is_configured()):
        print("\n--- Alert previews (Telegram not sent) ---")
        for it in results:
            print("\n" + alerts.format_ipo_alert(it))

    return results
