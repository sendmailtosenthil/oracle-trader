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

    # Small price book: the top-ranked candidates (enough to fill the pool with a
    # buffer) plus current holdings — never the full universe.
    from common.database import MomentumHolding
    held_syms = [h.symbol for h in db.query(MomentumHolding).filter(MomentumHolding.shares > 0).all()]
    top_syms = [r["symbol"] for r in ranking["ranked"][:60]]
    pb = H.price_book(set(top_syms) | set(held_syms))
    plan = strategy.build_plan(db, ranking, ranking["as_of"], price_book=pb)

    st.write(f"Plan as of **{plan['as_of']}** — type: **{plan['type'].upper()}**")
    st.caption("Ranking and buy/sell prices use the **latest candle close** "
               f"({plan['as_of']}) — today's running close during market hours, "
               "or the day's settled close otherwise.")

    # Bi-weekly cadence reminder (manual: this just flags when a rebalance is due).
    from common.database import MomentumTrade
    last_trade = (db.query(MomentumTrade.date)
                  .order_by(MomentumTrade.date.desc()).first())
    if last_trade and last_trade[0]:
        try:
            last_d = datetime.date.fromisoformat(last_trade[0])
            due_d = last_d + datetime.timedelta(days=int(cfg.rebalance_days))
            today = datetime.date.today()
            if today >= due_d:
                st.warning(f"🗓️ Rebalance **due** — last was {last_d} ({(today - last_d).days}d ago); "
                           f"cadence is every {cfg.rebalance_days}d.")
            else:
                st.caption(f"🗓️ Last rebalanced {last_d}; next due **{due_d}** "
                           f"(in {(due_d - today).days}d, {cfg.rebalance_days}-day cadence).")
        except (ValueError, TypeError):
            pass

    if plan["type"] == "hold":
        st.success(plan.get("note", "Nothing to do — all holdings within threshold."))
        return

    pool = plan["pool"]
    n_target = plan["n_target"]
    capital = plan["capital"]
    reserve = plan.get("reserve", 5)
    per_part = capital / max(1, n_target)
    max_del = min(reserve, n_target)

    if plan["type"] == "deploy":
        basis = f"₹{capital:,.0f} capital ÷ {n_target} stocks (both editable in **Settings**)"
    else:
        basis = (f"pot ₹{capital:,.0f} (sell proceeds + idle cash) ÷ {n_target} replacement(s) "
                 "— dynamic: smaller pot ⇒ smaller per-stock allocation")
    st.caption(f"🎯 Target capital per stock: **₹{per_part:,.0f}** — {basis}.")

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

    # --- Buy selection: top n_target active + reserves; Delete to substitute ---
    st.subheader(f"Buy — top {n_target} active, {max(0, len(pool) - n_target)} reserve")
    st.info(f"Tick **Delete** to skip a stock (e.g. priced far above the "
            f"₹{per_part:,.0f} target); the next reserve (rank {n_target + 1}+) takes its "
            f"place. Up to **{max_del}** deletes. Edit **Price** to match your fills.")

    sig = f"{plan['type']}:{plan['as_of']}:{n_target}:{len(pool)}"
    seldf = pd.DataFrame([{
        "Rank": p["rank"], "Symbol": p["symbol"], "Score": round(p["score"], 2),
        "Price": round(p["price"], 2), "Delete": False,
    } for p in pool])
    sed = st.data_editor(
        seldf, hide_index=True, use_container_width=True, num_rows="fixed",
        disabled=["Rank", "Symbol", "Score"], key=f"mom_pool_{sig}",
        column_config={
            "Score": st.column_config.NumberColumn(format="%.2f",
                     help="Risk-adjusted momentum score (blended return ÷ volatility)."),
            "Price": st.column_config.NumberColumn(min_value=0.0, step=0.05, format="%.2f"),
            "Delete": st.column_config.CheckboxColumn(
                      help="Skip this stock and pull in the next reserve."),
        },
    )
    excluded = [r["Symbol"] for _, r in sed.iterrows() if r["Delete"]]
    price_ovr = {r["Symbol"]: float(r["Price"] or 0) for _, r in sed.iterrows()}
    if len(excluded) > max_del:
        order = {p["symbol"]: i for i, p in enumerate(pool)}
        excluded = sorted(excluded, key=lambda s: order.get(s, 1e9))[:max_del]
        st.error(f"At most {max_del} deletes allowed — honoring the first {max_del} by rank; "
                 "untick the extras.")

    res = strategy.allocate_active(pool, excluded, n_target, capital,
                                   price_overrides=price_ovr,
                                   round_up=(plan["type"] == "replace"))
    edited_buys = res["buys"]
    active_syms = {p["symbol"] for p in res["active"]}
    excluded_set = set(excluded)
    cost_by = {b["symbol"]: b["cost"] for b in edited_buys}
    shares_by = {b["symbol"]: b["shares"] for b in edited_buys}

    # Allocation result over the whole pool: active (cost coloured), reserves
    # (grey, unused), deleted (struck through).
    rows = []
    for p in pool:
        sym = p["symbol"]
        status = ("Deleted" if sym in excluded_set
                  else "Active" if sym in active_syms else "Reserve")
        cost = cost_by.get(sym, 0.0)
        rows.append({
            "Rank": p["rank"], "Symbol": sym, "Score": round(p["score"], 2),
            "Price": round(price_ovr.get(sym, p["price"]), 2), "Shares": shares_by.get(sym, 0),
            "Cost": round(cost, 0), "Deployable": round(per_part, 0),
            "Gap": round(per_part - cost, 0) if status == "Active" else 0,
            "Status": status,
        })
    rdf = pd.DataFrame(rows)

    def _style(row):
        if row["Status"] == "Deleted":
            return ["text-decoration: line-through; color: #9e9e9e"] * len(row)
        if row["Status"] == "Reserve":
            return ["color: #9e9e9e"] * len(row)
        styles = [""] * len(row)
        i = rdf.columns.get_loc("Cost")
        if row["Cost"] > row["Deployable"]:
            styles[i] = "color: #c0392b; font-weight: 700"
        elif 0 < row["Cost"] < row["Deployable"]:
            styles[i] = "color: #1b5e20; font-weight: 700"
        return styles

    styled = (rdf.style.apply(_style, axis=1)
              .format({"Score": "{:.2f}", "Price": "₹{:,.2f}", "Cost": "₹{:,.0f}",
                       "Deployable": "₹{:,.0f}", "Gap": "₹{:,.0f}"}))
    st.caption("🟢 under target · 🔴 over target · grey = reserve (unused) · ~~struck~~ = deleted.")
    st.dataframe(styled, use_container_width=True, hide_index=True)

    # --- Recompute the summary from the active buys + edited sells ---
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
        # Holdings changed (not the universe ranking) — next load reads fresh from DB.
        st.success(f"Done — {len(edited_buys)} buy(s), {len(edited_sells)} sell(s) recorded.")
        st.rerun()


# --------------------------------------------------------------------------
def _do_refresh(db, broker, fetch_syms, from_date, prune_to=None, with_delivery=False):
    """Run a price refresh from ``from_date`` → today, recompute + persist the
    ranking to the DB, store the summary, and rerun.

    ``prune_to`` (a symbol set) trims the cache to exactly that set after fetching;
    pass it for a full build (drop stale leftovers). Leave it None for a light
    top-N refresh, which must NOT delete the rest of the universe's history.
    ``with_delivery`` fetches the one-per-day NSE delivery bhavcopy (settled data).
    """
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
    if not result["fatal"]:
        if prune_to is not None:
            result["pruned"] = mdata.prune_cache(prune_to)
        if with_delivery:
            with st.spinner("Fetching NSE delivery % (one bhavcopy)…"):
                try:
                    result["delivery"] = strategy.refresh_delivery(db)
                except Exception as exc:  # noqa: BLE001
                    result["delivery"] = {"ok": False, "error": str(exc)}
        with st.spinner("Re-ranking…"):
            strategy.compute_ranking(db)   # streams prices, persists ranking to DB
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
            dlv = last.get("delivery")
            if dlv and dlv.get("ok"):
                st.caption(f"Delivery %: {dlv['count']} stocks for {dlv['date']}"
                           + (" (already stored)" if dlv.get("skipped") else " (fetched)") + ".")
            elif dlv and not dlv.get("ok"):
                st.caption(f"⚠️ Delivery bhavcopy not fetched: {dlv.get('error')}")
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

    # Full universe set: current Nifty 500 ∪ holdings — used by the one-time build.
    from common.database import MomentumHolding
    current = {mdata.to_yahoo(s) for s in mdata.Universe.load().latest()}
    held = {h.symbol for h in db.query(MomentumHolding).filter(MomentumHolding.shares > 0).all()}
    full_syms = sorted(current | held)

    # Decide readiness from whether ranking ACTUALLY produces results — the
    # current universe must have enough history, not just *some* cached symbol.
    ranking = H.get_ranking(db)
    has_ranking = bool(ranking["ranked"])

    # Light daily set: top 50 by EITHER ranking (vol-adjusted or raw) ∪ holdings.
    TOP_N = 50
    top = {r["symbol"] for r in ranking["ranked"]
           if r["rank"] <= TOP_N or (r.get("raw_rank") or 10 ** 9) <= TOP_N}
    light_syms = sorted(top | held)

    # Market hours (IST): open on a weekday between 09:15 and 15:30. After close
    # (or pre-open / weekend) prices are settled, so refresh the FULL 500; during
    # the session do the light top-N (intraday closes still move).
    now_ist = datetime.datetime.now(mdata.IST)
    market_open = (now_ist.weekday() < 5
                   and datetime.time(9, 15) <= now_ist.time() <= datetime.time(15, 30))
    from_date = datetime.date.fromisoformat(latest_bar) if latest_bar else full_start

    st.divider()
    # --- PRIMARY: daily refresh — light intraday, full after close ---
    st.subheader("🔄 Refresh latest prices")
    if has_ranking:
        if market_open:
            st.caption(f"**Market open** — fetches the **top {TOP_N}** by either ranking "
                       f"(vol-adjusted or raw) + your {len(held)} holding(s) = "
                       f"**{len(light_syms)}** stocks and re-ranks. Light on Zerodha; a single "
                       "intraday move rarely shifts holdings/rebalance, so this gives "
                       "approximate PnL + near-boundary slippage. The rest keep their last "
                       "close. After **3:30 PM IST** this becomes a full 500 refresh.")
            if st.button("🔄 Refresh now (top names + holdings)", type="primary"):
                # No prune — the other ~450 names keep their history for re-ranking.
                _do_refresh(db, broker, light_syms, from_date)
        else:
            st.caption(f"**Market closed** — fetches the **full {len(full_syms)}** "
                       "(current Nifty 500 + holdings) on settled closes, re-ranks, and "
                       "prunes stale names. Use this for the accurate end-of-day ranking.")
            if st.button("🔄 Refresh now (full 500 — settled closes)", type="primary"):
                _do_refresh(db, broker, full_syms, from_date, prune_to=set(full_syms),
                            with_delivery=True)
    else:
        st.warning("⚠️ Ranking can't be computed yet — the current Nifty 500 names "
                   "lack the ~1-year of daily history the lookback needs.")
        summary = H.exclusion_summary(ranking)
        if summary:
            st.caption(f"Universe of {ranking.get('n_universe', 0)} excluded — {summary}.")
        st.caption("Build the full history once below (fetch from ~15 months back), then "
                   "the daily **Refresh now** will appear here.")

    # --- SECONDARY: one-time full history build / repair (with date picker) ---
    with st.expander("⚙️ Build / repair full price history (one-time)", expanded=not has_ranking):
        st.caption(f"Loads ~15 months of daily candles for the full universe "
                   f"({len(full_syms)} stocks: current Nifty 500 + holdings) so the "
                   "3/6/9-month lookback exists. Needed for first setup or to repair gaps — "
                   "not for daily use. Prunes the cache to this set (drops stale names).")
        history_from = st.date_input(
            "Fetch history from", value=full_start, max_value=today, format="YYYY-MM-DD",
            help="Start date for the historical daily candles (end is always today).",
        )
        if st.button("⬇️ Fetch full history from Zerodha"):
            _do_refresh(db, broker, full_syms, history_from, prune_to=set(full_syms),
                        with_delivery=True)


# --------------------------------------------------------------------------
def _render_config(db, cfg):
    st.write("Strategy parameters (ported from quant-momentum's `config.js`).")
    _MODELS = {
        "risk_adjusted": "Risk-adjusted (blended return ÷ daily volatility)",
        "clenow": "Clenow trend (annualised log-slope × R² — smooth, continuous)",
        "obv": "OBV-confirmed (momentum + on-balance-volume accumulation)",
        "delivery": "Delivery-confirmed (momentum + NSE delivery % — real buying)",
        "blended": "Blended (Clenow trend 60% + delivery 20% + OBV 20%)",
    }
    with st.form("momentum_config"):
        model_keys = list(_MODELS.keys())
        cur_model = cfg.scoring_model if cfg.scoring_model in _MODELS else "risk_adjusted"
        scoring_model = st.selectbox(
            "Scoring model", model_keys, index=model_keys.index(cur_model),
            format_func=lambda k: _MODELS[k],
            help="risk_adjusted: classic Sharpe-like. clenow: rewards the straightest "
                 "uptrend (R²). obv: blends momentum with volume accumulation.",
        )
        cc1, cc2 = st.columns(2)
        clenow_days = cc1.number_input("Clenow window (calendar days)",
                                       min_value=40, max_value=400, value=int(cfg.clenow_days),
                                       step=10, help="Clenow model only. Calendar span; uses the "
                                       "trading days within. ~180 ≈ 6 months.")
        rebalance_days = cc2.number_input("Rebalance every (calendar days)",
                                          min_value=1, max_value=120, value=int(cfg.rebalance_days),
                                          step=1, help="Cadence reminder on the Plan tab. 14 = bi-weekly.")
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
        cfg.scoring_model = scoring_model
        cfg.clenow_days = int(clenow_days)
        cfg.rebalance_days = int(rebalance_days)
        cfg.replace_rank_threshold = int(threshold)
        cfg.reinvest_idle_cash = bool(reinvest)
        cfg.vol_enabled = bool(vol_enabled)
        cfg.vol_months = int(vol_months)
        cfg.min_history_coverage = float(min_cov)
        cfg.factors_json = json.dumps(facs)
        strategy.recalc_cash(db, cfg)  # investment change affects cash identity
        db.commit()
        # Scoring params changed — recompute + persist the ranking to the DB.
        if mdata.cache_meta()[0] > 0:
            with st.spinner("Re-ranking…"):
                strategy.compute_ranking(db)
        st.success("Settings saved.")
        st.rerun()
