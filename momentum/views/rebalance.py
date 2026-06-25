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

    if plan.get("per_part"):
        if plan["type"] == "deploy":
            basis = (f"₹{plan['investable']:,.0f} capital ÷ {len(plan['buys'])} stocks "
                     "(both editable in the **Settings** tab)")
        else:
            basis = (f"pot ₹{plan['investable']:,.0f} (sell proceeds + idle cash) ÷ "
                     f"{len(plan['buys'])} replacement(s)")
        st.caption(f"🎯 Target capital per stock: **₹{plan['per_part']:,.0f}** — {basis}. "
                   "(Replace sizing is dynamic: a smaller pot ⇒ smaller per-stock allocation.)")

    st.info("✏️ You can **edit Shares and Price** below before executing (e.g. to match "
            "your actual fills). Cost, charges, injection and cash recompute from your edits.")

    # --- Sells (editable Shares / Price) ---
    edited_sells = []
    if plan["sells"]:
        st.subheader(f"Sell ({len(plan['sells'])})")
        avg_by = {s["symbol"]: (s.get("avg_cost") or 0.0) for s in plan["sells"]}
        meta_by = {s["symbol"]: s for s in plan["sells"]}
        sdf = pd.DataFrame([{
            "Symbol": s["symbol"], "Rank": s["rank"], "Reason": s["reason"],
            "Shares": int(s["shares"]), "Price": round(s["price"], 2) if s["price"] else 0.0,
        } for s in plan["sells"]])
        es = st.data_editor(
            sdf, hide_index=True, use_container_width=True, num_rows="fixed",
            disabled=["Symbol", "Rank", "Reason"], key="mom_sell_editor",
            column_config={
                "Shares": st.column_config.NumberColumn(min_value=0, step=1),
                "Price": st.column_config.NumberColumn(min_value=0.0, step=0.05, format="%.2f"),
            },
        )
        for _, r in es.iterrows():
            shares, price = int(r["Shares"] or 0), float(r["Price"] or 0)
            if shares <= 0 or price <= 0:
                continue
            chg = strategy.zerodha_charges(price, shares, "sell")
            avg = avg_by.get(r["Symbol"], 0.0)
            edited_sells.append({
                "symbol": r["Symbol"], "rank": meta_by[r["Symbol"]]["rank"],
                "reason": meta_by[r["Symbol"]]["reason"], "shares": shares, "price": price,
                "avg_cost": avg, "charges": chg, "pnl": (price - avg) * shares - chg,
            })

    # --- Buys (editable Shares / Price) ---
    edited_buys = []
    if plan["buys"]:
        st.subheader(f"Buy ({len(plan['buys'])})")
        rank_by = {b["symbol"]: b.get("rank") for b in plan["buys"]}
        bdf = pd.DataFrame([{
            "Symbol": b["symbol"], "Rank": b["rank"], "Shares": int(b["shares"]),
            "Price": round(b["price"], 2),
        } for b in plan["buys"]])
        eb = st.data_editor(
            bdf, hide_index=True, use_container_width=True, num_rows="fixed",
            disabled=["Symbol", "Rank"], key="mom_buy_editor",
            column_config={
                "Shares": st.column_config.NumberColumn(min_value=0, step=1),
                "Price": st.column_config.NumberColumn(min_value=0.0, step=0.05, format="%.2f"),
            },
        )
        for _, r in eb.iterrows():
            shares, price = int(r["Shares"] or 0), float(r["Price"] or 0)
            if shares <= 0 or price <= 0:
                continue
            chg = strategy.zerodha_charges(price, shares, "buy")
            edited_buys.append({
                "symbol": r["Symbol"], "rank": rank_by.get(r["Symbol"]), "shares": shares,
                "price": price, "cost": shares * price, "charges": chg,
            })

    # --- Recompute the summary from the (possibly edited) rows ---
    sell_net = sum(s["price"] * s["shares"] - s["charges"] for s in edited_sells)
    buy_cost = sum(b["cost"] for b in edited_buys)
    all_charges = sum(b["charges"] for b in edited_buys) + sum(s["charges"] for s in edited_sells)
    available = cfg.cash + sell_net
    needed = buy_cost + sum(b["charges"] for b in edited_buys)
    injection = max(0.0, needed - available)
    cash_left = available + injection - needed

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sell proceeds (net)", f"₹{sell_net:,.0f}")
    c2.metric("Buy cost", f"₹{buy_cost:,.0f}")
    c3.metric("Charges", f"₹{all_charges:,.2f}")
    c4.metric("Capital injection", f"₹{injection:,.0f}",
              help="Extra capital needed beyond cash + sell proceeds to fund the buys.")
    st.caption(f"Cash on hand ₹{cfg.cash:,.0f} + net proceeds ₹{sell_net:,.0f} − buys "
               f"₹{needed:,.0f} → **cash left ₹{cash_left:,.0f}** (injection ₹{injection:,.0f}).")

    st.divider()
    label = "Deploy initial portfolio" if plan["type"] == "deploy" else "Execute rebalance"
    st.warning("This records trades and updates holdings/cash from the values above. It does "
               "**not** place live broker orders — execute those manually on Kite.")
    if st.button(f"✅ {label}", type="primary"):
        if not edited_buys and not edited_sells:
            st.error("Nothing to execute — set at least one buy/sell with shares > 0.")
            return
        eplan = {"type": plan["type"], "as_of": plan["as_of"],
                 "sells": edited_sells, "buys": edited_buys, "injection": injection}
        strategy.execute_plan(db, eplan)
        H.clear_caches()
        st.success(f"Done — {len(edited_buys)} buy(s), {len(edited_sells)} sell(s) recorded.")
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
    # Keep the cache to exactly the fetched set (current 500 ∪ holdings) — drop
    # stale/delisted leftovers so the count and coverage stay honest.
    if not result["fatal"]:
        result["pruned"] = mdata.prune_cache(fetch_syms)
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
            pruned = last.get("pruned") or 0
            extra = f" · pruned {pruned} stale symbol(s)" if pruned else ""
            st.success(f"Updated {last['updated']} symbol(s), skipped {last['skipped']} "
                       f"· prices as of **{ts}**{extra}.")
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
    extra_held = len(held - current)
    st.caption(f"Fetch set: current Nifty 500 ({len(current)})"
               + (f" + {extra_held} held name(s) outside the index" if extra_held else "")
               + f" = **{len(fetch_syms)}** stocks. The cache is pruned to exactly this "
               "set on each refresh (stale names removed).")

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
