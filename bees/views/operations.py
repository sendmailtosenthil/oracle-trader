"""Operations Desk page: batch switches, SIP/top-ups, manual overrides, withdrawals."""
import datetime

import streamlit as st

from common.database import Strategy, CashFlow, PendingSwitch, Trade, recalculate_portfolio_from_ledger
from bees.services.charges import reconcile_strategy_charges


def render(db, strategies):
    st.title("Operations Desk")

    tab1, tab2, tab3, tab4 = st.tabs(
        ["Active Batch Switches", "Log SIP / Top-Up", "Manual Trade Override", "Withdraw Cash (SWP)"]
    )

    with tab1:
        _render_batch_switches(db)
    with tab2:
        _render_sip(db, strategies)
    with tab3:
        _render_manual_override(db, strategies)
    with tab4:
        _render_swp(db, strategies)


def _render_batch_switches(db):
    st.subheader("Pending Strategy Switches")
    pending_switches = db.query(PendingSwitch).filter(PendingSwitch.status == 'PENDING').all()

    if not pending_switches:
        st.info("No active switches pending! You are perfectly aligned with the Donchian channel.")
        return

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
                        if switch.units_sold_so_far >= switch.total_units_to_sell * 0.999:  # Account for floating point
                            switch.status = 'COMPLETED'  # marks the switch complete -> drives the pledge charge
                            st.success("Switch fully completed!")
                        else:
                            st.success(f"Batch logged. Remaining units: {switch.total_units_to_sell - switch.units_sold_so_far:.2f}")

                        db.commit()
                        recalculate_portfolio_from_ledger(db, strat.id)
                        reconcile_strategy_charges(db, strat.id)
                        st.rerun()
                    else:
                        st.error("Please enter valid quantities.")


def _render_sip(db, strategies):
    st.subheader("Log Fresh Cash (SIP / Top-Up)")
    st.write("Adding fresh cash into your strategy will accurately update your XIRR and Invested Capital.")

    target_strat = st.selectbox("Select Strategy", [s.name for s in strategies], key="strat_sip")
    strat = next(s for s in strategies if s.name == target_strat)
    with st.form(key="sip_form"):
        sip_date = st.date_input("Investment Date", value=datetime.date.today())

        col1, col2 = st.columns(2)
        with col1:
            asset1_units = st.number_input(f"New Units Bought: {strat.asset1}", min_value=0.0, step=1.0)
            asset1_price = st.number_input(f"Buy Price ({strat.asset1})", min_value=0.0, step=0.01)
        with col2:
            asset2_units = st.number_input(f"New Units Bought: {strat.asset2}", min_value=0.0, step=1.0)
            asset2_price = st.number_input(f"Buy Price ({strat.asset2})", min_value=0.0, step=0.01)

        # Total invested is derived from units x price (no manual amount entry).
        amount = asset1_units * asset1_price + asset2_units * asset2_price
        st.caption(f"**Total Investment (auto-calculated):** ₹{amount:,.2f}")

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
                reconcile_strategy_charges(db, strat.id)
                st.success(f"SIP of ₹{amount:,.2f} recorded successfully!")
            else:
                st.error("Enter units and buy price for at least one asset.")


def _render_manual_override(db, strategies):
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
            reconcile_strategy_charges(db, strat_man.id)
            st.success("Portfolio units forcefully overridden and synced to reality!")
            st.rerun()


def _render_swp(db, strategies):
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
                reconcile_strategy_charges(db, strat_swp.id)
                st.success(f"Successfully recorded withdrawal of ₹{withdrawal_amount:.2f}")
                st.rerun()
            else:
                st.error("Units and Price must be greater than 0.")
