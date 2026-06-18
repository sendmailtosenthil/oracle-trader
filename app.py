import streamlit as st
import pandas as pd
import datetime
import plotly.graph_objects as go
from pyxirr import xirr
import yfinance as yf
import pytz
import requests

from database import get_db, Strategy, Portfolio, CashFlow, PendingSwitch, Trade, BrokerConfig, recalculate_portfolio_from_ledger
from auth import require_auth, logout
from donchian import evaluate_donchian_intraday

st.set_page_config(page_title="Project Oracle", layout="wide")

# Shrink metric font size and global font to prevent cutoff
st.markdown("""
<style>
html, body, [class*="css"] {
    font-size: 14px !important;
}
div[data-testid="stMetricValue"] {
    font-size: 1.2rem !important;
}
</style>
""", unsafe_allow_html=True)

# Force Authentication
require_auth()

def calculate_xirr(db, strategy_id, current_value):
    cash_flows = db.query(CashFlow).filter(CashFlow.strategy_id == strategy_id).all()
    if not cash_flows:
        return 0.0
        
    dates = [cf.date.date() for cf in cash_flows]
    amounts = [cf.amount for cf in cash_flows]
    
    dates.append(datetime.datetime.today().date())
    amounts.append(current_value)
    
    try:
        calculated = xirr(dates, amounts)
        return (calculated * 100) if calculated else 0.0
    except:
        return 0.0

def get_reference_close(ticker):
    df = yf.download(ticker, period='5d', progress=False)
    if df.empty: return 0.0
    
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.datetime.now(ist)
        
    if df.empty: return 0.0
    close_series = df['Close']
    if isinstance(close_series, pd.DataFrame):
        close_series = close_series[ticker]
    close_series = close_series.dropna()
    return float(close_series.iloc[-1])

def get_asset_metrics(ticker):
    df = yf.download(ticker, period='5d', interval='1m', progress=False)
    if df.empty: return 0.0, 0.0
    
    close = df['Close'] if 'Close' in df else df.iloc[:, 3]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close.dropna()
    
    if close.empty: return 0.0, 0.0
    
    if close.index.tz is None:
        close.index = close.index.tz_localize('Asia/Kolkata')
    else:
        close.index = close.index.tz_convert('Asia/Kolkata')
        
    daily_close = close.resample('D').last().dropna()
        
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.datetime.now(ist)
        
    if len(daily_close) >= 2:
        return float(daily_close.iloc[-1]), float(daily_close.iloc[-2])
    elif len(daily_close) == 1:
        return float(daily_close.iloc[-1]), float(daily_close.iloc[-1])
        
    return 0.0, 0.0

st.sidebar.title(f"Welcome, {st.session_state['username']}")
if st.sidebar.button("Logout"):
    logout()

page = st.sidebar.radio("Navigation", ["Dashboard", "Operations (SIP / Batches)", "Ledger & History", "Broker Setup"])

db = next(get_db())
strategies = db.query(Strategy).all()

@st.cache_data(ttl=3600, show_spinner=False)
def is_zerodha_token_valid(enctoken):
    if not enctoken:
        return False
    try:
        headers = {"Authorization": f"enctoken {enctoken}"}
        res = requests.get("https://kite.zerodha.com/oms/user/profile/full", headers=headers, timeout=3)
        return res.status_code == 200
    except:
        return False

# Global Broker Check
broker_config = db.query(BrokerConfig).filter(BrokerConfig.broker_name == 'ZERODHA').first()
if not broker_config or not is_zerodha_token_valid(broker_config.enctoken):
    st.warning("🚨 **Zerodha Token Expired or Missing!** Your `enctoken` is invalid. Please navigate to the **Broker Setup** tab to update it.")

if page == "Dashboard":
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

elif page == "Operations (SIP / Batches)":
    st.title("Operations Desk")
    
    tab1, tab2, tab3, tab4 = st.tabs(["Active Batch Switches", "Log SIP / Top-Up", "Manual Trade Override", "Withdraw Cash (SWP)"])
    
    with tab1:
        st.subheader("Pending Strategy Switches")
        pending_switches = db.query(PendingSwitch).filter(PendingSwitch.status == 'PENDING').all()
        
        if not pending_switches:
            st.info("No active switches pending! You are perfectly aligned with the Donchian channel.")
        else:
            for switch in pending_switches:
                strat = db.query(Strategy).filter(Strategy.id == switch.strategy_id).first()
                from_ticker = strat.asset1 if switch.from_asset == 'ASSET1' else strat.asset2
                to_ticker = strat.asset1 if switch.to_asset == 'ASSET1' else strat.asset2
                
                remaining = switch.total_units_to_sell - switch.units_sold_so_far
                
                with st.expander(f"🚨 {strat.name}: Switch {from_ticker} ➡️ {to_ticker}", expanded=True):
                    st.write(f"**Total Units Needed to Sell:** {switch.total_units_to_sell:.2f}")
                    st.write(f"**Units Sold So Far:** {switch.units_sold_so_far:.2f}")
                    st.write(f"**Remaining to Sell:** {remaining:.2f}")
                    
                    st.markdown("---")
                    st.write("**Log a Fractional Batch Execution:**")
                    with st.form(key=f"batch_form_{switch.id}"):
                        exec_date = st.date_input("Execution Date", value=datetime.date.today())
                        units_sold = st.number_input(f"Units of {from_ticker} Sold", max_value=float(remaining), value=0.0, step=1.0)
                        sell_price = st.number_input(f"Selling Price per unit ({from_ticker})", min_value=0.0, step=0.01)
                        units_bought = st.number_input(f"Units of {to_ticker} Bought", min_value=0.0, step=1.0)
                        buy_price = st.number_input(f"Buying Price per unit ({to_ticker})", min_value=0.0, step=0.01)
                        
                        submit = st.form_submit_button("Log Batch Execution", type="primary")
                        if submit:
                            if units_sold > 0 and units_bought > 0:
                                # Log Exact Trades
                                date_obj = datetime.datetime.combine(exec_date, datetime.datetime.min.time())
                                db.add(Trade(strategy_id=strat.id, date=date_obj, asset=switch.from_asset, trade_type='SELL', units=units_sold, price=sell_price))
                                db.add(Trade(strategy_id=strat.id, date=date_obj, asset=switch.to_asset, trade_type='BUY', units=units_bought, price=buy_price))
                                
                                # Log any cash remainder / infusion to keep XIRR perfect
                                sold_val = units_sold * sell_price
                                bought_val = units_bought * buy_price
                                net_cash = sold_val - bought_val
                                if abs(net_cash) > 0.01:
                                    db.add(CashFlow(strategy_id=strat.id, date=date_obj, amount=net_cash, flow_type='RESIDUAL'))
                                                                    
                                switch.units_sold_so_far += units_sold
                                if switch.units_sold_so_far >= switch.total_units_to_sell * 0.999: # Account for floating point
                                    switch.status = 'COMPLETED'
                                    st.success("Switch fully completed!")
                                else:
                                    st.success(f"Batch logged. Remaining units: {switch.total_units_to_sell - switch.units_sold_so_far:.2f}")
                                
                                db.commit()
                                recalculate_portfolio_from_ledger(db, strat.id)
                                st.rerun()
                            else:
                                st.error("Please enter valid quantities.")
                                
    with tab2:
        st.subheader("Log Fresh Cash (SIP / Top-Up)")
        st.write("Adding fresh cash into your strategy will accurately update your XIRR and Invested Capital.")
        
        target_strat = st.selectbox("Select Strategy", [s.name for s in strategies], key="strat_sip")
        with st.form(key="sip_form"):
            sip_date = st.date_input("Investment Date", value=datetime.date.today())
            amount = st.number_input("Total Cash Added (₹)", min_value=0.0, step=100.0)
            
            strat = next(s for s in strategies if s.name == target_strat)
            col1, col2 = st.columns(2)
            with col1:
                asset1_units = st.number_input(f"New Units Bought: {strat.asset1}", min_value=0.0, step=1.0)
                asset1_price = st.number_input(f"Buy Price ({strat.asset1})", min_value=0.0, step=0.01)
            with col2:
                asset2_units = st.number_input(f"New Units Bought: {strat.asset2}", min_value=0.0, step=1.0)
                asset2_price = st.number_input(f"Buy Price ({strat.asset2})", min_value=0.0, step=0.01)
            
            if st.form_submit_button("Record Top-Up", type="primary"):
                if amount > 0:
                    date_obj = datetime.datetime.combine(sip_date, datetime.datetime.min.time())
                    
                    # Log the Trades
                    if asset1_units > 0:
                        db.add(Trade(strategy_id=strat.id, date=date_obj, asset='ASSET1', trade_type='BUY', units=asset1_units, price=asset1_price))
                    if asset2_units > 0:
                        db.add(Trade(strategy_id=strat.id, date=date_obj, asset='ASSET2', trade_type='BUY', units=asset2_units, price=asset2_price))
                    
                    # Log cash flow for XIRR (negative means money left our pocket)
                    cf = CashFlow(strategy_id=strat.id, date=date_obj, amount=-amount, flow_type='SIP')
                    db.add(cf)
                    db.commit()
                    recalculate_portfolio_from_ledger(db, strat.id)
                    st.success("SIP Recorded successfully!")
                else:
                    st.error("Amount must be greater than 0.")
                    
    with tab3:
        st.subheader("Manual Trade Override (Correcting Mistakes)")
        st.write("Use this if you accidentally executed a wrong trade on your broker and need to update the system units to match reality, regardless of Donchian signals.")
        
        target_strat_man = st.selectbox("Select Strategy", [s.name for s in strategies], key="strat_override")
        with st.form(key="manual_override_form"):
            override_date = st.date_input("Trade Date", value=datetime.date.today())
            
            strat_man = next(s for s in strategies if s.name == target_strat_man)
            
            # Use columns for layout
            col1, col2 = st.columns(2)
            with col1:
                st.markdown(f"**Sell {strat_man.asset1}**")
                units_sold_1 = st.number_input(f"Units Sold ({strat_man.asset1})", min_value=0.0, step=1.0)
                price_sold_1 = st.number_input(f"Sell Price ({strat_man.asset1})", min_value=0.0, step=0.01)
            with col2:
                st.markdown(f"**Buy {strat_man.asset2}**")
                units_bought_2 = st.number_input(f"Units Bought ({strat_man.asset2})", min_value=0.0, step=1.0)
                price_bought_2 = st.number_input(f"Buy Price ({strat_man.asset2})", min_value=0.0, step=0.01)
                
            st.markdown("---")
            col3, col4 = st.columns(2)
            with col3:
                st.markdown(f"**Sell {strat_man.asset2}**")
                units_sold_2 = st.number_input(f"Units Sold ({strat_man.asset2})", min_value=0.0, step=1.0)
                price_sold_2 = st.number_input(f"Sell Price ({strat_man.asset2})", min_value=0.0, step=0.01)
            with col4:
                st.markdown(f"**Buy {strat_man.asset1}**")
                units_bought_1 = st.number_input(f"Units Bought ({strat_man.asset1})", min_value=0.0, step=1.0)
                price_bought_1 = st.number_input(f"Buy Price ({strat_man.asset1})", min_value=0.0, step=0.01)

            if st.form_submit_button("Force Portfolio Update", type="primary"):
                # Log Trades and collect net cash diff
                date_obj = datetime.datetime.combine(override_date, datetime.datetime.min.time())
                sold_val = 0.0
                bought_val = 0.0
                
                if units_sold_1 > 0: 
                    db.add(Trade(strategy_id=strat_man.id, date=date_obj, asset='ASSET1', trade_type='SELL', units=units_sold_1, price=price_sold_1))
                    sold_val += units_sold_1 * price_sold_1
                if units_bought_1 > 0: 
                    db.add(Trade(strategy_id=strat_man.id, date=date_obj, asset='ASSET1', trade_type='BUY', units=units_bought_1, price=price_bought_1))
                    bought_val += units_bought_1 * price_bought_1
                if units_sold_2 > 0: 
                    db.add(Trade(strategy_id=strat_man.id, date=date_obj, asset='ASSET2', trade_type='SELL', units=units_sold_2, price=price_sold_2))
                    sold_val += units_sold_2 * price_sold_2
                if units_bought_2 > 0: 
                    db.add(Trade(strategy_id=strat_man.id, date=date_obj, asset='ASSET2', trade_type='BUY', units=units_bought_2, price=price_bought_2))
                    bought_val += units_bought_2 * price_bought_2
                
                net_cash = sold_val - bought_val
                if abs(net_cash) > 0.01:
                    db.add(CashFlow(strategy_id=strat_man.id, date=date_obj, amount=net_cash, flow_type='RESIDUAL'))
                
                db.commit()
                recalculate_portfolio_from_ledger(db, strat_man.id)
                st.success("Portfolio units forcefully overridden and synced to reality!")
                st.rerun()

    with tab4:
        st.subheader("Withdraw Cash (SWP)")
        st.write("Log a withdrawal to take cash out of your strategy. This will record a SELL trade and a positive Cash Flow.")
        
        target_strat_swp = st.selectbox("Select Strategy", [s.name for s in strategies], key="strat_swp")
        strat_swp = next(s for s in strategies if s.name == target_strat_swp)
        asset_to_sell = st.selectbox("Asset to Sell", [strat_swp.asset1, strat_swp.asset2], key="asset_swp")
        
        with st.form(key="swp_form"):
            swp_date = st.date_input("Withdrawal Date", value=datetime.date.today(), key="swp_date")
            
            col1, col2 = st.columns(2)
            with col1:
                units_to_sell = st.number_input(f"Units Sold ({asset_to_sell})", min_value=0.0, step=1.0)
            with col2:
                sell_price = st.number_input(f"Sell Price ({asset_to_sell})", min_value=0.0, step=0.01)
                
            if st.form_submit_button("Log Withdrawal", type="primary"):
                if units_to_sell > 0 and sell_price > 0:
                    withdrawal_amount = units_to_sell * sell_price
                    date_obj = datetime.datetime.combine(swp_date, datetime.datetime.min.time())
                    
                    # Log Sell Trade
                    asset_code = 'ASSET1' if asset_to_sell == strat_swp.asset1 else 'ASSET2'
                    db.add(Trade(strategy_id=strat_swp.id, date=date_obj, asset=asset_code, trade_type='SELL', units=units_to_sell, price=sell_price))
                    
                    # Log Cash Flow (Positive = Withdrawal)
                    db.add(CashFlow(strategy_id=strat_swp.id, date=date_obj, amount=withdrawal_amount, flow_type='SWP'))
                    
                    db.commit()
                    recalculate_portfolio_from_ledger(db, strat_swp.id)
                    st.success(f"Successfully recorded withdrawal of ₹{withdrawal_amount:.2f}")
                    st.rerun()
                else:
                    st.error("Units and Price must be greater than 0.")

elif page == "Ledger & History":
    st.title("Trade Ledger & History")
    st.write("View and inline-edit all your executed trades. Changes here will instantly recalculate your live portfolio.")
    
    target_strat_ledger = st.selectbox("Select Strategy to View", [s.name for s in strategies], key="strat_ledger")
    strat_ledger = next(s for s in strategies if s.name == target_strat_ledger)
    
    # Holdings Summary
    st.subheader("Holdings Summary")
    portfolios = db.query(Portfolio).filter(Portfolio.strategy_id == strat_ledger.id).all()
    port1 = next(p for p in portfolios if p.asset == 'ASSET1')
    port2 = next(p for p in portfolios if p.asset == 'ASSET2')
    
    # Calculate Realized PNL by looping over history
    trades_for_pnl = db.query(Trade).filter(Trade.strategy_id == strat_ledger.id).order_by(Trade.date.asc()).all()
    realized_pnl = {'ASSET1': 0.0, 'ASSET2': 0.0}
    avg_p = {'ASSET1': 0.0, 'ASSET2': 0.0}
    u_held = {'ASSET1': 0.0, 'ASSET2': 0.0}
    
    for t in trades_for_pnl:
        asset = t.asset
        if t.trade_type == 'BUY':
            cost = t.units * t.price
            new_u = u_held[asset] + t.units
            if new_u > 0:
                avg_p[asset] = ((u_held[asset] * avg_p[asset]) + cost) / new_u
            u_held[asset] = new_u
        elif t.trade_type == 'SELL':
            realized_pnl[asset] += (t.price - avg_p[asset]) * t.units
            u_held[asset] -= t.units
    
    h_col1, h_col2 = st.columns(2)
    for i, (port, ticker, asset_code) in enumerate([(port1, strat_ledger.asset1, 'ASSET1'), (port2, strat_ledger.asset2, 'ASSET2')]):
        curr_price, prev_close = get_asset_metrics(ticker)
        invested = port.invested_amount
        current_val = port.units * curr_price
        
        # 1. Unrealized PNL
        unrealized_pnl = current_val - invested
        unrealized_pnl_pct = (unrealized_pnl / invested * 100) if invested > 0 else 0.0
        
        # 2. Realized PNL
        real_pnl = realized_pnl[asset_code]
        
        # 3. Total Overall PNL
        total_overall_pnl = unrealized_pnl + real_pnl
        total_overall_pct = (total_overall_pnl / invested * 100) if invested > 0 else 0.0
        
        # 4. 1-Day PNL
        day_pnl = (curr_price - prev_close) * port.units
        day_pnl_pct = ((curr_price / prev_close) - 1) * 100 if prev_close > 0 else 0.0
        
        with (h_col1 if i == 0 else h_col2):
            st.markdown(f"**{ticker}** ({port.units:.2f} Units)")
            m1, m2 = st.columns(2)
            m1.metric("Amount Invested", f"₹{invested:,.2f}")
            m2.metric("Unrealized PNL", f"₹{unrealized_pnl:,.2f}", f"{unrealized_pnl_pct:.2f}%")
            
            m3, m4, m5 = st.columns(3)
            
            # Custom metric to support exact Blue/Red requirements
            real_color = "#0068c9" if real_pnl >= 0 else "#ff2b2b"
            real_icon = "↑ Booked" if real_pnl >= 0 else "↓ Loss"
            
            m3.markdown(f"""
            <div style="display: flex; flex-direction: column; line-height: 1.2; padding-top: 0.1rem;">
                <span style="font-size: 14px; color: var(--text-color); opacity: 0.7; padding-bottom: 0.3rem;">Realized PNL</span>
                <span style="font-size: 1.2rem; color: var(--text-color); font-weight: 400; padding-bottom: 0.3rem;">₹{real_pnl:,.2f}</span>
                <span style="font-size: 14px; font-weight: 500; color: {real_color};">{real_icon}</span>
            </div>
            """, unsafe_allow_html=True)
            
            m4.metric("Total Overall PNL", f"₹{total_overall_pnl:,.2f}", f"{total_overall_pct:.2f}%")
            m5.metric("1-Day PNL", f"₹{day_pnl:,.2f}", f"{day_pnl_pct:.2f}%")
            
    st.divider()
    
    # Render CashFlows
    st.subheader("Cash Flows (Investments & Withdrawals)")
    cashflows = db.query(CashFlow).filter(CashFlow.strategy_id == strat_ledger.id).order_by(CashFlow.date.asc()).all()
    cf_data = [{'_id': c.id, 'Date': c.date, 'Amount': c.amount, 'Type': c.flow_type} for c in cashflows]
    cf_df = pd.DataFrame(cf_data)
    
    net_cashflow = sum(c.amount for c in cashflows)
    total_invested_cash = -net_cashflow
    
    st.markdown(f"**Total Net Capital Invested (Cash Inflows - Outflows):** ₹{total_invested_cash:,.2f}")
    
    if not cf_df.empty:
        cf_columns_config = {
            '_id': None, # Hide
            'Date': st.column_config.DatetimeColumn("Date", required=True),
            'Amount': st.column_config.NumberColumn("Amount", required=True, step=0.01),
            'Type': st.column_config.SelectboxColumn("Type", options=["INITIAL", "SIP", "SWP", "RESIDUAL"], required=True)
        }

        def color_amount(val):
            if pd.isna(val) or val == 0:
                return ''
            return 'color: #00FF00' if val > 0 else 'color: #FF0000'
            
        cf_styled = cf_df.style.map(color_amount, subset=['Amount'])
        
        edited_cfs = st.data_editor(
            cf_styled,
            column_config=cf_columns_config,
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            key="cf_editor"
        )
        
        if st.button("Save Cash Flow Changes", type="primary", key="save_cf"):
            db.query(CashFlow).filter(CashFlow.strategy_id == strat_ledger.id).delete()
            for _, row in edited_cfs.iterrows():
                db.add(CashFlow(
                    strategy_id=strat_ledger.id,
                    date=row['Date'],
                    amount=float(row['Amount']),
                    flow_type=row['Type']
                ))
            db.commit()
            st.success("Cash Flows updated successfully!")
            st.rerun()
    else:
        st.info("No cash flows recorded yet.")
    
    # Render Trades
    st.subheader("Trade Ledger")
    trades = db.query(Trade).filter(Trade.strategy_id == strat_ledger.id).order_by(Trade.date.asc()).all()
    
    # Calculate PnL on the fly for display
    trade_data = []
    avg_price = {'ASSET1': 0.0, 'ASSET2': 0.0}
    units_held = {'ASSET1': 0.0, 'ASSET2': 0.0}
    
    for t in trades:
        row = {
            '_id': t.id,
            'Date': t.date,
            'Asset': strat_ledger.asset1 if t.asset == 'ASSET1' else strat_ledger.asset2,
            '_internal_asset': t.asset,
            'Action': '🟢 BUY' if t.trade_type == 'BUY' else '🔴 SELL',
            'Units': t.units,
            'Price': t.price,
            'Total Value': t.units * t.price,
            'Realized PnL': 0.0
        }
        
        asset = t.asset
        if t.trade_type == 'BUY':
            cost = t.units * t.price
            new_u = units_held[asset] + t.units
            if new_u > 0:
                avg_price[asset] = ((units_held[asset] * avg_price[asset]) + cost) / new_u
            units_held[asset] = new_u
        elif t.trade_type == 'SELL':
            row['Realized PnL'] = (t.price - avg_price[asset]) * t.units
            units_held[asset] -= t.units
            
        trade_data.append(row)
        
    trade_df = pd.DataFrame(trade_data)
    
    if not trade_df.empty:
        total_trade_value = sum(t['Total Value'] for t in trade_data)
        total_realized_pnl = sum(t['Realized PnL'] for t in trade_data)
        
        st.markdown(f"**Gross Traded Value:** ₹{total_trade_value:,.2f} &nbsp;&nbsp;|&nbsp;&nbsp; **Total Realized PnL (All Time):** ₹{total_realized_pnl:,.2f}")
        
        # Hide internal columns and make certain columns editable
        columns_config = {
            '_id': None, # Hide
            '_internal_asset': None, # Hide
            'Date': st.column_config.DatetimeColumn("Date", required=True),
            'Asset': st.column_config.SelectboxColumn("Asset", options=[strat_ledger.asset1, strat_ledger.asset2], required=True),
            'Action': st.column_config.SelectboxColumn("Action", options=["🟢 BUY", "🔴 SELL"], required=True),
            'Units': st.column_config.NumberColumn("Units", min_value=0.0, step=0.01, required=True),
            'Price': st.column_config.NumberColumn("Price", min_value=0.0, step=0.01, required=True),
            'Total Value': st.column_config.NumberColumn("Total Value", disabled=True, format="%.2f"),
            'Realized PnL': st.column_config.NumberColumn("Realized PnL", disabled=True, format="%.2f")
        }

        def color_pnl(val):
            if pd.isna(val) or val == 0:
                return ''
            return 'color: #00FF00' if val > 0 else 'color: #FF0000'
            
        styled_df = trade_df.style.map(color_pnl, subset=['Realized PnL'])
        
        edited_trades = st.data_editor(
            styled_df, 
            column_config=columns_config, 
            num_rows="dynamic", 
            use_container_width=True, 
            hide_index=True,
            key="trade_editor"
        )
        
        if st.button("Save Ledger Changes & Recalculate Portfolio", type="primary"):
            db.query(Trade).filter(Trade.strategy_id == strat_ledger.id).delete()
            
            for _, row in edited_trades.iterrows():
                asset_code = 'ASSET1' if row['Asset'] == strat_ledger.asset1 else 'ASSET2'
                action_code = 'BUY' if 'BUY' in row['Action'] else 'SELL'
                
                new_trade = Trade(
                    strategy_id=strat_ledger.id,
                    date=row['Date'],
                    asset=asset_code,
                    trade_type=action_code,
                    units=float(row['Units']),
                    price=float(row['Price'])
                )
                db.add(new_trade)
            
            db.commit()
            recalculate_portfolio_from_ledger(db, strat_ledger.id)
            
            st.success("Ledger updated successfully! Live portfolio exactly matches your trade history.")
            st.rerun()
    else:
        st.info("No trades found. Start by executing a batch or logging an override.")

elif page == "Broker Setup":
    st.title("Broker Setup & Integrations")
    st.write("Configure your API keys and tokens for broker integration.")
    
    st.subheader("Zerodha / Kite")
    
    # Get existing config or create an empty one
    broker_config = db.query(BrokerConfig).filter(BrokerConfig.broker_name == 'ZERODHA').first()
    
    current_user_id = broker_config.user_id if broker_config else 'PC8006'
    current_enctoken = broker_config.enctoken if broker_config else ''
    
    with st.form(key="zerodha_config_form"):
        z_user_id = st.text_input("Zerodha User ID", value=current_user_id)
        z_enctoken = st.text_input("Kite enctoken", value=current_enctoken, type="password")
        
        if st.form_submit_button("Save Broker Configuration", type="primary"):
            if z_enctoken.strip() == "":
                st.error("enctoken cannot be empty.")
            else:
                if broker_config:
                    broker_config.user_id = z_user_id
                    broker_config.enctoken = z_enctoken
                else:
                    new_config = BrokerConfig(broker_name='ZERODHA', user_id=z_user_id, enctoken=z_enctoken)
                    db.add(new_config)
                
                db.commit()
                is_zerodha_token_valid.clear()
                st.success("Zerodha credentials saved successfully!")
                st.rerun()
