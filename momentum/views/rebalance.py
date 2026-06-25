"""Rebalance page: preview the buy/sell plan, execute it, refresh prices, tune config."""
import datetime
import json

import pandas as pd
import streamlit as st

from common.database import BrokerConfig
from common.broker import is_zerodha_token_valid
from momentum.services import data as mdata
from momentum.services import strategy
from momentum.views import _helpers as H


def render(db):
    st.title("🔁 Momentum — Rebalance")

    cfg = strategy.get_config(db)
    tab_plan, tab_data, tab_cfg = st.tabs(["Rebalance plan", "Refresh prices", "Settings"])

    with tab_plan:
        _render_plan(db, cfg)
    with tab_data:
        _render_refresh(db, cfg)
    with tab_cfg:
        _render_config(db, cfg)


# --------------------------------------------------------------------------
def _render_plan(db, cfg):
    n_files, _, latest_bar, _earliest = mdata.cache_meta()
    if n_files == 0:
        st.error("No cached prices. Use the **Refresh prices** tab first.")
        return

    ranking = H.get_ranking(db)
    if not ranking["ranked"]:
        H.render_no_ranking(ranking)
        return

    pb = H.price_book()
    plan = strategy.build_plan(db, ranking, ranking["as_of"], price_book=pb)

    st.write(f"Plan as of **{plan['as_of']}** — type: **{plan['type'].upper()}**")
    st.caption("Ranking and buy/sell prices use the **latest candle close** "
               f"({plan['as_of']}) — today's running close during market hours, "
               "or the day's settled close otherwise.")

    if plan["type"] == "hold":
        st.success(plan.get("note", "Nothing to do — all holdings within threshold."))
        return

    if plan["sells"]:
        st.subheader(f"Sell ({len(plan['sells'])})")
        sdf = pd.DataFrame([{
            "Symbol": s["symbol"], "Rank": s["rank"], "Reason": s["reason"],
            "Shares": s["shares"], "Price": round(s["price"], 2) if s["price"] else None,
            "Proceeds": round(s["price"] * s["shares"], 0) if s["price"] else None,
            "Est. P/L": round(s["pnl"], 0) if s["pnl"] is not None else None,
        } for s in plan["sells"]])
        st.dataframe(sdf, use_container_width=True, hide_index=True)

    if plan["buys"]:
        st.subheader(f"Buy ({len(plan['buys'])})")
        bdf = pd.DataFrame([{
            "Symbol": b["symbol"], "Rank": b["rank"], "Shares": b["shares"],
            "Price": round(b["price"], 2), "Cost": round(b["cost"], 0),
        } for b in plan["buys"]])
        st.dataframe(bdf, use_container_width=True, hide_index=True)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Proceeds", f"₹{plan['proceeds']:,.0f}")
    c2.metric("Investable", f"₹{plan['investable']:,.0f}",
              help="Sell proceeds + idle cash (reinvest mode)." if cfg.reinvest_idle_cash
              else "Sell proceeds only.")
    c3.metric("Capital injection", f"₹{plan['injection']:,.0f}",
              help="Extra capital needed to satisfy the min-1-share rule.")
    c4.metric("Cash left", f"₹{plan['cash_left']:,.0f}")

    st.divider()
    label = ("Deploy initial portfolio" if plan["type"] == "deploy"
             else "Execute rebalance")
    st.warning("This records trades and updates holdings/cash. It does **not** place "
               "live broker orders — execute those manually on Kite.")
    if st.button(f"✅ {label}", type="primary"):
        strategy.execute_plan(db, plan)
        st.success(f"Done — {len(plan['buys'])} buy(s), {len(plan['sells'])} sell(s) recorded.")
        st.rerun()


# --------------------------------------------------------------------------
def _do_refresh(broker, fetch_syms, from_date):
    """Run a price refresh from ``from_date`` → today, persist the summary, rerun."""
    log_box = st.empty()
    msgs = []

    def cb(m):
        msgs.append(m)
        log_box.code("\n".join(msgs[-12:]))

    with st.spinner("Fetching daily candles from Zerodha..."):
        result = mdata.refresh_prices(
            enctoken=broker.enctoken, user_id=broker.user_id,
            symbols=fetch_syms, history_from=from_date, progress_cb=cb,
        )
    H.clear_caches()
    st.session_state["mom_refresh_result"] = result
    st.rerun()


def _render_refresh(db, cfg):
    st.write("Pull the latest daily close for the Nifty 500 and recompute the ranking. "
             "Prices are fetched **only when you click** — never automatically.")

    # Summary of the previous refresh (survives the rerun we trigger so the
    # metrics below reflect the just-written cache).
    last = st.session_state.pop("mom_refresh_result", None)
    if last:
        if last["fatal"]:
            st.error(f"Fatal: {last['fatal']}")
        else:
            ts = mdata.format_fetched(last.get("fetched_at"))
            st.success(f"Updated {last['updated']} symbol(s), skipped {last['skipped']} "
                       f"· prices as of **{ts}**.")
            if last["errors"]:
                with st.expander(f"{len(last['errors'])} warning(s)"):
                    st.write("\n".join(last["errors"][:50]))

    n_files, fetched_at, latest_bar, earliest_bar = mdata.cache_meta()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cached symbols", n_files)
    c2.metric("🕒 Latest price as of", mdata.format_fetched(fetched_at),
              help="When the prices in the cache were last pulled (IST). Daily bars "
                   "have no intraday time, so this fetch moment is the price as-of "
                   "stamp. Shows a full time after your next refresh.")
    c3.metric("History from", earliest_bar or "—", help="Earliest cached bar (lookback depth).")
    c4.metric("Latest bar", latest_bar or "—", help="Date of the most recent price bar.")

    # Constituents-file health: auto-fetch the official list when missing/stale.
    ensure = H.auto_refresh_constituents()
    if ensure.get("action") == "fetched":
        st.success(f"Auto-updated Nifty 500 constituents → snapshot "
                   f"{ensure.get('fetched_date')} ({ensure.get('count')} stocks).")
    elif ensure.get("action") == "failed":
        st.warning("⚠️ Couldn't auto-update constituents from NSE "
                   f"({ensure.get('error')}). Using the existing file; you can run "
                   "`python scripts/fetch_nifty500_constituents.py` manually.")

    status = mdata.universe_status()
    if not status["ok"]:
        st.error("🚨 Cannot use the Nifty 500 constituents file — " + status["message"])
        return
    if status["stale"]:
        st.warning("⚠️ " + status["message"])

    broker = db.query(BrokerConfig).filter(BrokerConfig.broker_name == 'ZERODHA').first()
    if not broker or not is_zerodha_token_valid(broker.enctoken, broker.user_id):
        st.error("🚨 Zerodha enctoken missing or expired. Set it in **Broker Setup** first.")
        return
    st.success("Zerodha token is valid.")

    today = datetime.date.today()
    full_start = today - datetime.timedelta(days=455)

    # Fetch the CURRENT index membership plus any held stocks (so holdings that
    # have left the index still get priced). ~500 names in practice.
    from common.database import MomentumHolding
    current = {mdata.to_yahoo(s) for s in mdata.Universe.load().latest()}
    held = {h.symbol for h in db.query(MomentumHolding).filter(MomentumHolding.shares > 0).all()}
    fetch_syms = sorted(current | held)

    # Decide readiness from whether ranking ACTUALLY produces results — the
    # current universe must have enough history, not just *some* cached symbol.
    # (earliest_bar above is the oldest across all 634 cached names, which can be
    # old leftovers, so it's a misleading readiness signal.)
    ranking = H.get_ranking(db)
    has_ranking = bool(ranking["ranked"])

    st.divider()
    # --- PRIMARY: daily latest-price refresh (no date to pick) ---
    st.subheader("🔄 Refresh latest prices")
    if has_ranking:
        st.caption(f"Fetches today's close for all {len(fetch_syms)} stocks "
                   f"(tops up from the last bar **{latest_bar}** → today) and re-ranks. "
                   "This is the daily action — no historical re-download.")
        if st.button("🔄 Refresh now (latest prices)", type="primary"):
            _do_refresh(broker, fetch_syms, datetime.date.fromisoformat(latest_bar))
    else:
        st.warning("⚠️ Ranking can't be computed yet — the current Nifty 500 names "
                   "lack the ~1-year of daily history the lookback needs.")
        summary = H.exclusion_summary(ranking)
        if summary:
            st.caption(f"Universe of {ranking.get('n_universe', 0)} excluded — {summary}.")
        st.caption("Build the history once below (fetch from ~15 months back), then "
                   "the daily **Refresh now** will appear here.")

    # --- SECONDARY: one-time history build / repair (with date picker) ---
    with st.expander("⚙️ Build / repair price history (one-time)", expanded=not has_ranking):
        st.caption("Loads ~15 months of daily candles so the 3/6/9-month lookback "
                   "exists. Needed only for first setup or to repair gaps — not for "
                   "daily use. New bars merge in; existing history is kept.")
        history_from = st.date_input(
            "Fetch history from", value=full_start, max_value=today, format="YYYY-MM-DD",
            help="Start date for the historical daily candles (end is always today).",
        )
        if st.button("⬇️ Fetch history from Zerodha"):
            _do_refresh(broker, fetch_syms, history_from)


# --------------------------------------------------------------------------
def _render_config(db, cfg):
    st.write("Strategy parameters (ported from quant-momentum's `config.js`).")
    with st.form("momentum_config"):
        c1, c2 = st.columns(2)
        investment = c1.number_input("Initial capital (₹)", min_value=1000.0,
                                     value=float(cfg.investment), step=1000.0)
        num_stocks = c2.number_input("Number of stocks", min_value=1, max_value=50,
                                     value=int(cfg.num_stocks), step=1)
        c3, c4 = st.columns(2)
        threshold = c3.number_input("Replace when rank >", min_value=1, max_value=500,
                                    value=int(cfg.replace_rank_threshold), step=5)
        reinvest = c4.checkbox("Reinvest idle cash on rebalance",
                               value=bool(cfg.reinvest_idle_cash))
        c5, c6, c7 = st.columns(3)
        vol_enabled = c5.checkbox("Volatility-adjust score", value=bool(cfg.vol_enabled))
        vol_months = c6.number_input("Volatility lookback (months)", min_value=1, max_value=12,
                                     value=int(cfg.vol_months), step=1)
        min_cov = c7.number_input("Min history coverage", min_value=0.0, max_value=1.0,
                                  value=float(cfg.min_history_coverage), step=0.05)
        factors_json = st.text_area(
            "Lookback factors (JSON: months + weight, auto-normalised)",
            value=cfg.factors_json, height=100,
        )
        submitted = st.form_submit_button("Save settings", type="primary")

    if submitted:
        try:
            facs = json.loads(factors_json)
            assert isinstance(facs, list) and all("months" in f and "weight" in f for f in facs)
        except (ValueError, AssertionError):
            st.error("Factors must be a JSON list of {\"months\": N, \"weight\": W}.")
            return
        cfg.investment = investment
        cfg.num_stocks = int(num_stocks)
        cfg.replace_rank_threshold = int(threshold)
        cfg.reinvest_idle_cash = bool(reinvest)
        cfg.vol_enabled = bool(vol_enabled)
        cfg.vol_months = int(vol_months)
        cfg.min_history_coverage = float(min_cov)
        cfg.factors_json = json.dumps(facs)
        strategy.recalc_cash(db, cfg)  # investment change affects cash identity
        db.commit()
        H.clear_caches()
        st.success("Settings saved.")
        st.rerun()
