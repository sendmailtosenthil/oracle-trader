"""Dashboard page: live portfolio valuation, signals, and Donchian charts."""
import streamlit as st
import plotly.graph_objects as go

from common.database import Portfolio, CashFlow, Trade, PendingSwitch
from bees.donchian import evaluate_donchian_intraday
from bees.services.finance import calculate_xirr, total_charges, realized_pnl_by_asset


def render(db, strategies):
    st.title("Project Oracle: Live Portfolio")

    for strat in strategies:
        st.subheader(f"{strat.name} ({strat.window}-Day Donchian)")

        # Get live data
        with st.spinner("Fetching live prices..."):
            res = evaluate_donchian_intraday(strat.asset1, strat.asset2, strat.window)

        if not res:
            st.error("Failed to fetch market data.")
            continue

        portfolios = db.query(Portfolio).filter(Portfolio.strategy_id == strat.id).all()
        port1 = next(p for p in portfolios if p.asset == 'ASSET1')
        port2 = next(p for p in portfolios if p.asset == 'ASSET2')

        # Current market value of holdings
        total_val = port1.units * res['live_price1'] + port2.units * res['live_price2']

        # Cost basis of current holdings (= Cash Invested + realized profit reinvested via switches)
        total_invested = port1.invested_amount + port2.invested_amount

        # Net external money contributed (deposits - withdrawals)
        cash_invested = -sum(cf.amount for cf in db.query(CashFlow).filter(CashFlow.strategy_id == strat.id).all())

        # Realized profit booked from sells/switches (over full history)
        trades = db.query(Trade).filter(Trade.strategy_id == strat.id).order_by(Trade.date.asc()).all()
        realized = realized_pnl_by_asset(trades)
        realized_profit = realized['ASSET1'] + realized['ASSET2']

        unrealized_pnl = total_val - total_invested
        unrealized_pct = (unrealized_pnl / total_invested * 100) if total_invested > 0 else 0.0
        charges = total_charges(db, strat.id)
        roi = ((total_val / total_invested) - 1) * 100 if total_invested > 0 else 0
        strat_xirr = calculate_xirr(db, strat.id, total_val)

        # Live prices
        st.caption(f"**{strat.asset1}** ₹{res['live_price1']:.2f}  ·  **{strat.asset2}** ₹{res['live_price2']:.2f}")

        c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
        c1.metric("Cash Invested", f"₹{cash_invested:,.2f}",
                  help="Net money you actually contributed (deposits − withdrawals).")
        c2.metric("Total Invested", f"₹{total_invested:,.2f}",
                  help="Cost basis of current holdings = Cash Invested + realized profit reinvested through switches.")
        c3.metric("Current Value", f"₹{total_val:,.2f}", f"{roi:.2f}% ROI",
                  help="Live market value of holdings. ROI is vs Total Invested.")
        c4.metric("Realized Profit", f"₹{realized_profit:,.2f}",
                  help="Profit already booked from sells/switches (already included in Total Invested).")
        c5.metric("Unrealized Profit", f"₹{unrealized_pnl:,.2f}", f"{unrealized_pct:.2f}%",
                  help="Current Value − Total Invested.")
        c6.metric("Charges", f"₹{charges:,.2f}",
                  help="Total trading charges to date (already reflected in XIRR).")
        c7.metric("XIRR", f"{strat_xirr:.2f}%",
                  help="Annualized return from your cash flows, net of charges.")

        pending = db.query(PendingSwitch).filter(PendingSwitch.strategy_id == strat.id, PendingSwitch.status == 'PENDING').first()

        # Auto-heal: If user manually executed the switch via the Ledger, auto-complete the pending alert
        if pending:
            from_port = next(p for p in portfolios if p.asset == pending.from_asset)
            if from_port.units <= 0.0001:
                pending.status = 'COMPLETED'
                db.commit()
                pending = None

        if pending:
            target_ticker = strat.asset1 if pending.to_asset == 'ASSET1' else strat.asset2
            st.error(f"🚨 PENDING BATCH SWITCH: Switch all funds to **{target_ticker}**! Go to Operations to execute.")
        else:
            target_ticker = strat.asset1 if strat.current_signal_target == 'ASSET1' else strat.asset2
            st.success(f"✅ Holding steady in **{target_ticker}**.")

        # Chart
        df = res['df'].tail(90)
        show_guides = st.checkbox("Show channel guide lines (25% / 50% / 75%)", value=False, key=f"guides_{strat.id}")
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df.index, y=df['Ratio'], mode='lines', name='Ratio', line=dict(color='blue')))
        fig.add_trace(go.Scatter(x=df.index, y=df['Upper'], mode='lines', name='Upper', line=dict(color='green', dash='dash')))
        fig.add_trace(go.Scatter(x=df.index, y=df['Lower'], mode='lines', name='Lower', line=dict(color='red', dash='dash')))
        if show_guides:
            span = df['Upper'] - df['Lower']
            for frac, label in [(0.25, '25%'), (0.5, '50%'), (0.75, '75%')]:
                fig.add_trace(go.Scatter(
                    x=df.index, y=df['Lower'] + frac * span,
                    mode='lines', name=label,
                    line=dict(color='lightgrey', dash='dot', width=1),
                ))
        fig.update_layout(height=400, margin=dict(l=0, r=0, t=30, b=0))
        st.plotly_chart(fig, use_container_width=True)
        st.divider()
