"""Momentum dashboard: portfolio valuation, holdings, and the live top ranking."""
import pandas as pd
import streamlit as st

from momentum.services import data as mdata
from momentum.services import strategy
from momentum.views import _helpers as H


def render(db):
    st.title("📈 Momentum — Nifty500")

    cfg = strategy.get_config(db)
    # Self-heal the Nifty 500 universe (downloads only when missing/stale).
    H.auto_refresh_constituents()
    n_files, fetched_at, latest_bar, _earliest = mdata.cache_meta()

    has_prices = n_files > 0
    ranking = H.get_ranking(db) if has_prices else {"ranked": [], "as_of": None,
                                                    "snapshot_date": None, "n_universe": 0}
    rmap = H.rank_map(ranking)
    rrmap = H.raw_rank_map(ranking)
    pb = H.price_book() if has_prices else None
    pv = strategy.portfolio_value(db, rank_map=rmap, raw_rank_map=rrmap, price_book=pb)

    if not has_prices:
        st.info("No price cache yet — holdings are valued at **cost basis**. "
                "Refresh prices on the **Rebalance → Refresh prices** tab to get "
                "live valuations and momentum rankings.")
    else:
        st.caption(
            f"🕒 Prices fetched **{mdata.format_fetched(fetched_at)}**  ·  latest price "
            f"bar **{latest_bar or '—'}**  ·  ranking as of **{ranking['as_of']}**  ·  "
            f"universe snapshot **{ranking['snapshot_date']}** "
            f"({ranking['n_universe']} names, {len(ranking['ranked'])} ranked)  ·  "
            f"holding **{cfg.num_stocks}** stocks"
        )

    # --- Portfolio KPIs ---
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Equity", f"₹{pv['equity']:,.0f}", f"{pv['total_return_pct']:.2f}%",
              help="Cash + market value of holdings. Δ is total return vs invested capital.")
    c2.metric("Cash", f"₹{pv['cash']:,.0f}", help="Idle cash available to deploy.")
    c3.metric("Holdings value", f"₹{pv['holdings_value']:,.0f}")
    c4.metric("Invested", f"₹{pv['invested']:,.0f}",
              help="Initial capital + any min-1-share top-up injections.")
    c5.metric("Unrealized P/L", f"₹{pv['unrealized_pnl']:,.0f}")
    c6.metric("Realized P/L", f"₹{pv['realized_pnl']:,.0f}",
              help="Booked profit/loss from sells to date.")

    if pv["n_holdings"] == 0:
        st.info("No holdings yet. Go to **Rebalance** to deploy the initial portfolio "
                f"of {cfg.num_stocks} stocks.")
    else:
        st.subheader(f"Holdings ({pv['n_holdings']})")
        hdf = pd.DataFrame([{
            "Entry rank": h["entry_rank"],
            "Today rank (vol-adj)": h["rank"] if h["rank"] is not None else None,
            "Today rank (raw)": h.get("raw_rank"),
            "Rank Δ": (h["entry_rank"] - h["rank"])
                       if (h["rank"] is not None and h["entry_rank"] is not None) else None,
            "Symbol": h["symbol"], "Shares": h["shares"],
            "Buy price": round(h["avg_cost"], 2),
            "Invested": round(h["cost"], 0),
            "Charges": round(h["charges"], 2),
            "LTP": round(h["last"], 2) if h["last"] else None,
            "Cur. value": round(h["value"], 0),
            "P/L": round(h["pnl"], 0) if h["last"] else None,
            "P/L %": round(h["pnl_pct"], 2) if h["last"] else None,
            "Bought": h["entry_date"],
        } for h in pv["holdings"]])
        st.dataframe(hdf, use_container_width=True, hide_index=True)
        if not has_prices:
            st.caption("**LTP / Cur. value / P/L / Today rank** populate after you "
                       "**Refresh prices** (Rebalance tab). 'Rank Δ' = entry rank − today's rank "
                       "(positive = improved).")

        # Flag laggards whose rank has dropped beyond the replacement threshold
        # (only meaningful once a ranking has been computed).
        if ranking["ranked"]:
            laggards = [h for h in pv["holdings"]
                        if h["rank"] is None or h["rank"] > cfg.replace_rank_threshold]
            if laggards:
                names = ", ".join(f"{h['symbol']} (rank {h['rank'] or '—'})" for h in laggards)
                st.warning(f"🔁 {len(laggards)} holding(s) past rank {cfg.replace_rank_threshold} — "
                           f"consider a rebalance: {names}")
            else:
                st.success(f"✅ All holdings ranked within top {cfg.replace_rank_threshold}.")

    # --- Top momentum ranking (both orderings side by side) ---
    if not ranking["ranked"]:
        if has_prices:
            st.divider()
            H.render_no_ranking(ranking)
        return
    st.divider()
    st.subheader("Momentum ranking — risk-adjusted vs raw")
    top_n = st.slider("Show top N", 10, min(100, len(ranking["ranked"])),
                      min(30, len(ranking["ranked"])), step=5)
    held = {h["symbol"] for h in pv["holdings"]}

    col_vol, col_raw = st.columns(2)
    with col_vol:
        st.markdown("**Risk-adjusted (volatility)** — *score = blended ÷ daily vol*")
        vdf = pd.DataFrame([{
            "Rank": r["rank"], "Symbol": r["symbol"], "Score": round(r["value"], 2),
            "Raw rank": r.get("raw_rank"), "Held": "✓" if r["symbol"] in held else "",
        } for r in sorted(ranking["ranked"], key=lambda x: x["rank"])[:top_n]])
        st.dataframe(vdf, use_container_width=True, hide_index=True)
    with col_raw:
        st.markdown("**Raw momentum** — *score = blended 3m/6m/9m return %*")
        rdf = pd.DataFrame([{
            "Rank": r.get("raw_rank"), "Symbol": r["symbol"],
            "Score %": round(r["blended"] * 100, 2),
            "Vol rank": r["rank"], "Held": "✓" if r["symbol"] in held else "",
        } for r in sorted(ranking["ranked"], key=lambda x: x.get("raw_rank") or 1e9)[:top_n]])
        st.dataframe(rdf, use_container_width=True, hide_index=True)

    # Combined per-stock detail (factor breakdown), sorted by risk-adjusted rank.
    with st.expander("Per-stock detail (factor returns)"):
        ddf = pd.DataFrame([{
            "Vol rank": r["rank"], "Raw rank": r.get("raw_rank"), "Symbol": r["symbol"],
            "Vol score": round(r["value"], 2), "Raw score %": round(r["blended"] * 100, 2),
            "3m %": round(r.get("r3m", 0) * 100, 2), "6m %": round(r.get("r6m", 0) * 100, 2),
            "9m %": round(r.get("r9m", 0) * 100, 2),
            "Daily vol %": round(r["vol"] * 100, 2) if r.get("vol") else None,
            "Held": "✓" if r["symbol"] in held else "",
        } for r in ranking["ranked"][:top_n]])
        st.dataframe(ddf, use_container_width=True, hide_index=True)

    st.caption("**Raw momentum** = weighted 3m/6m/9m return (rewards biggest gainers). "
               "**Risk-adjusted** = that return ÷ daily volatility (rewards the smoothest "
               "gainers). Compare the two ranks to see which stocks owe their position to "
               "low volatility vs raw return.")
