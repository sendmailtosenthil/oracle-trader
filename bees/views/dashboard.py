"""Dashboard page: live portfolio valuation, signals, and Donchian charts."""
import streamlit as st
import plotly.graph_objects as go

from bees.database import Portfolio, PendingSwitch
from bees.donchian import evaluate_donchian_intraday
from bees.services.finance import calculate_xirr


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

        val1 = port1.units * res['live_price1']
        val2 = port2.units * res['live_price2']
        total_val = val1 + val2

        total_invested = port1.invested_amount + port2.invested_amount
        roi = ((total_val / total_invested) - 1) * 100 if total_invested > 0 else 0
        strat_xirr = calculate_xirr(db, strat.id, total_val)

        # Synchronize Unrealized PNL with Current Value so math always adds up
        unrealized_pnl = total_val - total_invested

        col1, col2, col3, col4, col5, col6 = st.columns(6)
        col1.metric(f"{strat.asset1} Price", f"₹{res['live_price1']:.2f}")
        col2.metric(f"{strat.asset2} Price", f"₹{res['live_price2']:.2f}")
        col3.metric("Total Invested", f"₹{total_invested:,.2f}")
        col4.metric("Current Value", f"₹{total_val:,.2f}", f"{roi:.2f}% ROI")
        col5.metric("Unrealized PnL", f"₹{unrealized_pnl:,.2f}", delta_color="normal" if unrealized_pnl >= 0 else "inverse")
        col6.metric("XIRR", f"{strat_xirr:.2f}%")

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
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df.index, y=df['Ratio'], mode='lines', name='Ratio', line=dict(color='blue')))
        fig.add_trace(go.Scatter(x=df.index, y=df['Upper'], mode='lines', name='Upper', line=dict(color='green', dash='dash')))
        fig.add_trace(go.Scatter(x=df.index, y=df['Lower'], mode='lines', name='Lower', line=dict(color='red', dash='dash')))
        fig.update_layout(height=400, margin=dict(l=0, r=0, t=30, b=0))
        st.plotly_chart(fig, use_container_width=True)
        st.divider()
