"""Read-only Streamlit dashboard over gse.db.

The jobs (snapshot / screener / IPO watcher) write the DB; this only reads it.
Run with:  streamlit run dashboard.py

Shows the disclaimer banner, the ranked watchlist (with factor values), a
factor-score chart, the IPO feed, and the snapshot date on everything so stale
data is always visible.
"""
import pandas as pd
import streamlit as st

import storage

DISCLAIMER = (
    "**Not financial advice.** This is a rules-based screener, not a predictor. "
    "It flags candidates to research, not buy. Verify any IPO against the official "
    "SEC-approved prospectus before any decision."
)

st.set_page_config(page_title="GSE Bot", page_icon="📈", layout="wide")


@st.cache_data(ttl=300)
def load_watchlist(run_date):
    return storage.get_watchlist(run_date)


@st.cache_data(ttl=300)
def load_snapshot(snap_date):
    return storage.get_snapshot(snap_date)


@st.cache_data(ttl=300)
def load_ipos(limit):
    return storage.recent_ipos(limit)


@st.cache_data(ttl=300)
def load_dates():
    return storage.watchlist_dates(), storage.distinct_snapshot_dates()


def main():
    storage.init_db()
    st.title("📈 GSE Bot — Screener & IPO Watch")
    st.warning(DISCLAIMER)

    wl_dates, snap_dates = load_dates()

    if not wl_dates:
        st.info(
            "No watchlist yet. Run `python main.py snapshot` then "
            "`python main.py run-screen` to populate the database."
        )
    else:
        # --- Watchlist -------------------------------------------------------
        st.subheader("Daily Watchlist")
        run_date = st.selectbox("Run date", wl_dates, index=0)
        st.caption(f"Snapshot date: **{run_date}** — figures are as of this date.")

        rows = load_watchlist(run_date)
        df = pd.DataFrame(rows)
        if not df.empty:
            df["liquidity"] = df["liquidity_flag"].map({1: "⚠️ thin/stale", 0: "ok"})
            view = df.rename(columns={
                "rank": "#", "symbol": "Symbol", "score": "Score",
                "pe": "P/E", "div_yield": "Div Yield", "momentum": "Momentum %",
            })[["#", "Symbol", "Score", "P/E", "Div Yield", "Momentum %", "liquidity"]]
            view = view.rename(columns={"liquidity": "Liquidity"})

            left, right = st.columns([3, 2])
            with left:
                st.dataframe(view, hide_index=True, use_container_width=True)
            with right:
                chart_df = df.set_index("symbol")["score"].sort_values(ascending=True)
                st.bar_chart(chart_df, horizontal=True, height=max(200, 28 * len(chart_df)))
            st.caption(
                "Score = weighted, normalized blend of available factors "
                "(value, income, momentum, liquidity). Weights are tunable in config.yaml. "
                "Factors are renormalized per symbol, so missing data (e.g. null P/E) "
                "doesn't penalize a stock."
            )

    # --- IPO feed ------------------------------------------------------------
    st.subheader("IPO / Listing Watch")
    ipos = load_ipos(50)
    if not ipos:
        st.info("No IPO candidates recorded yet. Run `python main.py run-ipo`.")
    else:
        idf = pd.DataFrame(ipos)
        for _, r in idf.iterrows():
            title = r["title"] or "(untitled)"
            url = r["url"]
            head = f"[{title}]({url})" if url else title
            st.markdown(f"- {head}  \n  _{r['source']} · first seen {r['first_seen']}_")
        st.caption(
            "Authoritative sources (SEC Ghana, GSE) are the truth; news outlets are "
            "early signals. Always confirm against the official notice/prospectus."
        )

    # --- Footer --------------------------------------------------------------
    st.divider()
    latest_snap = snap_dates[-1] if snap_dates else "none"
    st.caption(
        f"Data via dev.kwayisi.org/apis/gse · latest snapshot: {latest_snap} · "
        f"{len(snap_dates)} day(s) of history accumulated."
    )


if __name__ == "__main__":
    main()
