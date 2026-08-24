"""Transparent, tunable screener.

Computes per-symbol factors (value, income, momentum, liquidity), normalizes each
0-100 across the universe, then takes a weighted sum. Weights are renormalized
per-symbol over whichever factors are actually available, so a stock is never
penalized for data it cannot have (the GSE API leaves eps/dps null, and momentum
needs ~20 days of accumulated history).

No black-box ML — every score is explainable from its factor values.
"""
import math
from datetime import date

import config
import storage


def _pe(price, eps):
    if price is None or eps is None or eps <= 0:
        return None
    return price / eps


def _div_yield(price, dps):
    if price is None or dps is None or price <= 0:
        return None
    return dps / price


def _momentum(history, lookback):
    """Percent price return over `lookback` snapshots. None if not enough history."""
    prices = [h["price"] for h in history if h["price"] is not None]
    if len(prices) <= lookback:
        return None
    old, new = prices[-(lookback + 1)], prices[-1]
    if old is None or old <= 0:
        return None
    return (new - old) / old * 100.0


def _avg_volume(history, lookback):
    vols = [h["volume"] for h in history[-lookback:] if h["volume"] is not None]
    if not vols:
        return None
    return sum(vols) / len(vols)


def _liquidity_flag(history, zero_days):
    """True if the most recent `zero_days` snapshots all have ~0 volume."""
    recent = history[-zero_days:]
    if len(recent) < zero_days:
        return False
    return all((h["volume"] or 0) == 0 for h in recent)


def _normalize(values, invert=False):
    """Map a dict {symbol: raw} to {symbol: 0-100}. None values stay None.

    invert=True means lower-is-better (used for P/E).
    """
    present = {s: v for s, v in values.items() if v is not None}
    out = {s: None for s in values}
    if not present:
        return out
    lo, hi = min(present.values()), max(present.values())
    span = hi - lo
    for s, v in present.items():
        if span == 0:
            score = 50.0
        else:
            score = (v - lo) / span * 100.0
            if invert:
                score = 100.0 - score
        out[s] = score
    return out


def screen(run_date=None, cfg=None):
    """Run the screener for `run_date` (default today). Returns ranked list of dicts."""
    cfg = cfg or config.load()
    scfg = cfg["screener"]
    run_date = run_date or date.today().isoformat()

    snapshot = storage.get_snapshot(run_date)
    if not snapshot:
        return []

    mom_days = scfg["momentum_days"]
    liq_days = scfg["liquidity_days"]
    zero_days = scfg["zero_volume_days"]
    weights = scfg["weights"]

    # Raw factor inputs per symbol.
    raw_pe, raw_income, raw_mom, raw_vol = {}, {}, {}, {}
    flags, sectors = {}, {}
    lookback = max(mom_days, liq_days) + 1

    for row in snapshot:
        sym = row["symbol"]
        sectors[sym] = row["sector"]
        history = storage.get_symbol_history(sym, lookback)
        # Carry forward last-reported eps/dps when today's are null (e.g. the
        # fundamentals source is down). Combined with today's price this is a
        # correct trailing P/E / yield, so value/income keep scoring.
        eps, dps = row["eps"], row["dps"]
        if eps is None or dps is None:
            k_eps, k_dps = storage.last_known_fundamentals(sym, run_date)
            eps = eps if eps is not None else k_eps
            dps = dps if dps is not None else k_dps
        raw_pe[sym] = _pe(row["price"], eps)
        raw_income[sym] = _div_yield(row["price"], dps)
        raw_mom[sym] = _momentum(history, mom_days)
        raw_vol[sym] = _avg_volume(history, liq_days)
        flags[sym] = _liquidity_flag(history, zero_days)

    # Normalize each factor across the universe (P/E inverted: lower is better).
    # Volume is extremely skewed on the GSE (one or two names trade orders of
    # magnitude more than the rest), so a linear min-max collapses everyone below
    # the top name to ~0. Log-transform first for a meaningful spread; still fully
    # explainable (it's a monotonic transform of traded volume).
    log_vol = {s: (math.log1p(v) if v is not None else None) for s, v in raw_vol.items()}
    n_value = _normalize(raw_pe, invert=True)
    n_income = _normalize(raw_income)
    n_momentum = _normalize(raw_mom)
    n_liquidity = _normalize(log_vol)

    results = []
    for row in snapshot:
        sym = row["symbol"]
        factors = {
            "value": n_value[sym],
            "income": n_income[sym],
            "momentum": n_momentum[sym],
            "liquidity": n_liquidity[sym],
        }
        # Weighted sum over available factors only, with weights renormalized.
        avail = {k: v for k, v in factors.items() if v is not None}
        wsum = sum(weights[k] for k in avail)
        if wsum > 0:
            score = sum(factors[k] * weights[k] for k in avail) / wsum
        else:
            score = None

        results.append({
            "symbol": sym,
            "score": round(score, 2) if score is not None else None,
            "pe": round(raw_pe[sym], 2) if raw_pe[sym] is not None else None,
            "div_yield": round(raw_income[sym], 4) if raw_income[sym] is not None else None,
            "momentum": round(raw_mom[sym], 2) if raw_mom[sym] is not None else None,
            "liquidity_flag": 1 if flags[sym] else 0,
            "sector": sectors[sym],
            "factors_used": sorted(avail.keys()),
        })

    # Rank by score (None scores sink to the bottom).
    results.sort(key=lambda r: (r["score"] is not None, r["score"] or 0), reverse=True)
    for i, r in enumerate(results, start=1):
        r["rank"] = i
    return results


def run_and_save(run_date=None, cfg=None):
    """Screen, persist top-K to the watchlist table, return (full_ranked, top_k)."""
    cfg = cfg or config.load()
    run_date = run_date or date.today().isoformat()
    ranked = screen(run_date, cfg)
    if not ranked:
        return [], []

    k = cfg["screener"]["watchlist_size"]
    top = ranked[:k]
    storage.save_watchlist(
        [{
            "symbol": r["symbol"],
            "score": r["score"],
            "pe": r["pe"],
            "div_yield": r["div_yield"],
            "momentum": r["momentum"],
            "liquidity_flag": r["liquidity_flag"],
            "rank": r["rank"],
        } for r in top],
        run_date=run_date,
    )
    return ranked, top
