"""Momentum ledger: trade history, per-symbol analytics, and book maintenance."""
import pandas as pd
import streamlit as st

from common.database import MomentumTrade, MomentumHolding
from momentum.services import strategy


def render(db):
    st.title("📒 Momentum — Ledger & History")

    trades = (db.query(MomentumTrade)
              .order_by(MomentumTrade.date.desc(), MomentumTrade.id.desc())
              .all())
    if not trades:
        st.info("No momentum trades recorded yet. Deploy a portfolio from **Rebalance**.")
        _maintenance(db)
        return

    buys = [t for t in trades if t.side == "BUY"]
    sells = [t for t in trades if t.side == "SELL"]
    realized = sum(t.pnl or 0.0 for t in sells)
    wins = sum(1 for t in sells if (t.pnl or 0) > 0)
    total_charges = sum(t.charges or 0.0 for t in trades)

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Trades", len(trades))
    c2.metric("Buys", len(buys))
    c3.metric("Sells", len(sells))
    c4.metric("Charges", f"₹{total_charges:,.2f}",
              help="Total Zerodha charges paid (STT, txn, GST, stamp, DP).")
    c5.metric("Realized P/L", f"₹{realized:,.0f}", help="Net of charges.")
    c6.metric("Win rate", f"{(wins / len(sells) * 100):.0f}%" if sells else "—")

    st.subheader("Trade history")
    tdf = pd.DataFrame([{
        "Date": t.date, "Symbol": t.symbol, "Side": t.side, "Shares": t.shares,
        "Price": round(t.price, 2), "Value": round(t.value, 0),
        "Charges": round(t.charges or 0.0, 2), "Rank": t.rank,
        "P/L": round(t.pnl, 0) if t.pnl is not None else None, "Reason": t.reason,
    } for t in trades])
    st.dataframe(tdf, use_container_width=True, hide_index=True)

    # --- Per-symbol analytics ---
    st.subheader("By symbol")
    by_sym = {}
    for t in trades:
        s = by_sym.setdefault(t.symbol, {"buys": 0, "sells": 0, "invested": 0.0,
                                         "pnl": 0.0, "charges": 0.0})
        s["charges"] += t.charges or 0.0
        if t.side == "BUY":
            s["buys"] += 1
            s["invested"] += t.value
        else:
            s["sells"] += 1
            s["pnl"] += t.pnl or 0.0
    sym_df = pd.DataFrame([{
        "Symbol": sym, "Buys": s["buys"], "Sells": s["sells"],
        "Invested": round(s["invested"], 0), "Charges": round(s["charges"], 2),
        "Realized P/L": round(s["pnl"], 0),
    } for sym, s in sorted(by_sym.items(), key=lambda kv: kv[1]["invested"], reverse=True)])
    st.dataframe(sym_df, use_container_width=True, hide_index=True)

    _maintenance(db)


def _maintenance(db):
    st.divider()
    with st.expander("⚙️ Maintenance"):
        st.caption("Rebuild holdings & cash from the trade ledger, or wipe the "
                   "momentum books to start fresh.")
        c1, c2 = st.columns(2)
        if c1.button("Recalculate holdings from ledger"):
            strategy.recalc_holdings(db)
            strategy.recalc_cash(db)
            st.success("Holdings and cash recomputed from trades.")
            st.rerun()
        if c2.button("🗑️ Reset momentum books", type="secondary"):
            db.query(MomentumTrade).delete()
            db.query(MomentumHolding).delete()
            cfg = strategy.get_config(db)
            cfg.capital_injected = 0.0
            cfg.cash = cfg.investment
            db.commit()
            st.success("Momentum trades and holdings cleared.")
            st.rerun()
