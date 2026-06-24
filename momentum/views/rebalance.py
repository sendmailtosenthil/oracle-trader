"""Rebalance page: preview the buy/sell plan, execute it, refresh prices, tune config."""
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
        st.warning("No ranked stocks for the latest date.")
        return

    pb = H.price_book()
    plan = strategy.build_plan(db, ranking, ranking["as_of"], price_book=pb)

    st.write(f"Plan as of **{plan['as_of']}** — type: **{plan['type'].upper()}**")

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
def _render_refresh(db, cfg):
    st.write("Re-fetch daily OHLC price history from Zerodha for the Nifty500 "
             "universe and update the local cache used for ranking. New bars are "
             "**merged** into the cache (existing history is preserved).")

    # Show (and clear) the summary of the previous refresh, which survives the
    # rerun we trigger so the metrics below reflect the new cache.
    last = st.session_state.pop("mom_refresh_result", None)
    if last:
        if last["fatal"]:
            st.error(f"Fatal: {last['fatal']}")
        else:
            st.success(f"Updated {last['updated']} symbol(s), skipped {last['skipped']}.")
            if last["errors"]:
                with st.expander(f"{len(last['errors'])} warning(s)"):
                    st.write("\n".join(last["errors"][:50]))

    n_files, fetched_at, latest_bar, earliest_bar = mdata.cache_meta()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cached symbols", n_files)
    c2.metric("Last fetched", fetched_at or "—")
    c3.metric("History from", earliest_bar or "—")
    c4.metric("Latest bar", latest_bar or "—")

    # Constituents-file health: auto-fetch the official list when missing/stale,
    # then block refresh if we still can't use a valid list.
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
    st.caption(f"Universe: **{status['count']}** stocks (Nifty 500 snapshot "
               f"**{status['snapshot_date']}**).")

    broker = db.query(BrokerConfig).filter(BrokerConfig.broker_name == 'ZERODHA').first()
    if not broker or not is_zerodha_token_valid(broker.enctoken, broker.user_id):
        st.error("🚨 Zerodha enctoken missing or expired. Set it in **Broker Setup** first.")
        return
    st.success("Zerodha token is valid.")

    today = datetime.date.today()
    # Momentum needs ~9-month lookback (+buffer). If the cache lacks that history
    # (fresh start, or a prior short-range fetch truncated it), default the start
    # back ~15 months to rebuild; otherwise default to today for a daily top-up.
    full_start = today - datetime.timedelta(days=455)
    lookback_ok = bool(earliest_bar) and earliest_bar <= (today - datetime.timedelta(days=300)).isoformat()
    default_from = today if lookback_ok else full_start

    history_from = st.date_input(
        "Fetch history from", value=default_from, max_value=today, format="YYYY-MM-DD",
        help="Start date for the daily candles fetched (end is always today). "
             "Bars merge into the cache, so a daily top-up can start at today; "
             "for a first build or to repair history, pick ~15 months back.",
    )
    if not lookback_ok:
        st.warning(f"⚠️ Cached history is short (earliest bar: {earliest_bar or 'none'}). "
                   f"Momentum needs ~9 months — fetch from **{full_start.isoformat()}** "
                   "or earlier to (re)build the lookback before ranking will be meaningful.")

    # Fetch only the CURRENT index membership plus any held stocks (so holdings
    # that have since left the index still get priced). ~500 names in practice.
    from common.database import MomentumHolding
    current = {mdata.to_yahoo(s) for s in mdata.Universe.load().latest()}
    held = {h.symbol for h in db.query(MomentumHolding).filter(MomentumHolding.shares > 0).all()}
    fetch_syms = sorted(current | held)
    extra = len(held - current)
    st.caption(f"{len(fetch_syms)} symbols to fetch — current Nifty500 membership "
               f"({len(current)})" + (f" + {extra} held name(s) outside it" if extra else "")
               + ". Daily candles, rate-limited (a few minutes).")

    if st.button("⬇️ Refresh prices from Zerodha", type="primary"):
        log_box = st.empty()
        msgs = []

        def cb(m):
            msgs.append(m)
            log_box.code("\n".join(msgs[-12:]))

        with st.spinner("Fetching daily candles..."):
            result = mdata.refresh_prices(
                enctoken=broker.enctoken, user_id=broker.user_id,
                symbols=fetch_syms,
                history_from=history_from, progress_cb=cb,
            )
        H.clear_caches()
        # Persist the summary and rerun so the metrics above reflect the new cache.
        st.session_state["mom_refresh_result"] = result
        st.rerun()


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
