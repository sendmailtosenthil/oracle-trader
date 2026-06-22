"""Ledger & History page: holdings summary, editable cash flows, paginated trades."""
import pandas as pd
import streamlit as st

from bees.database import Portfolio, CashFlow, Trade, recalculate_portfolio_from_ledger
from bees.services.charges import compute_trade_charges
from bees.services.finance import realized_pnl_by_asset, realized_charges_by_asset
from bees.services.market_data import get_asset_metrics


def render(db, strategies):
    st.title("Trade Ledger & History")
    st.write("View and inline-edit all your executed trades. Changes here will instantly recalculate your live portfolio.")

    target_strat_ledger = st.selectbox("Select Strategy to View", [s.name for s in strategies], key="strat_ledger")
    strat_ledger = next(s for s in strategies if s.name == target_strat_ledger)

    _render_holdings_summary(db, strat_ledger)
    st.divider()
    _render_cash_flows(db, strat_ledger)
    _render_trade_ledger(db, strat_ledger)


def _render_holdings_summary(db, strat_ledger):
    st.subheader("Holdings Summary")
    portfolios = db.query(Portfolio).filter(Portfolio.strategy_id == strat_ledger.id).all()
    port1 = next(p for p in portfolios if p.asset == 'ASSET1')
    port2 = next(p for p in portfolios if p.asset == 'ASSET2')

    # Realized PnL and charges across full trade history (chronological)
    trades_for_pnl = db.query(Trade).filter(Trade.strategy_id == strat_ledger.id).order_by(Trade.date.asc()).all()
    realized_pnl = realized_pnl_by_asset(trades_for_pnl)
    charges_by_asset = realized_charges_by_asset(trades_for_pnl)

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

        # 3. Charges incurred on this asset
        asset_charges = charges_by_asset[asset_code]

        # 4. Net Overall PNL (after trading charges)
        total_overall_pnl = unrealized_pnl + real_pnl - asset_charges
        total_overall_pct = (total_overall_pnl / invested * 100) if invested > 0 else 0.0

        # 5. 1-Day PNL
        day_pnl = (curr_price - prev_close) * port.units
        day_pnl_pct = ((curr_price / prev_close) - 1) * 100 if prev_close > 0 else 0.0

        with (h_col1 if i == 0 else h_col2):
            st.markdown(f"**{ticker}** ({port.units:.2f} Units)")
            m1, m2 = st.columns(2)
            m1.metric("Amount Invested", f"₹{invested:,.2f}")
            m2.metric("Unrealized PNL", f"₹{unrealized_pnl:,.2f}", f"{unrealized_pnl_pct:.2f}%")

            m3, m4, m5, m6 = st.columns(4)

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

            m4.metric("Charges", f"₹{asset_charges:,.2f}")
            m5.metric("Net Overall PNL", f"₹{total_overall_pnl:,.2f}", f"{total_overall_pct:.2f}%")
            m6.metric("1-Day PNL", f"₹{day_pnl:,.2f}", f"{day_pnl_pct:.2f}%")


def _color_amount(val):
    if pd.isna(val) or val == 0:
        return ''
    return 'color: #00FF00' if val > 0 else 'color: #FF0000'


def _render_cash_flows(db, strat_ledger):
    st.subheader("Cash Flows (Investments & Withdrawals)")
    cashflows = db.query(CashFlow).filter(CashFlow.strategy_id == strat_ledger.id).order_by(CashFlow.date.asc()).all()
    cf_data = [{'_id': c.id, 'Date': c.date, 'Amount': c.amount, 'Type': c.flow_type} for c in cashflows]
    # Net summary over the full set (independent of pagination)
    net_cashflow = sum(c.amount for c in cashflows)
    total_invested_cash = -net_cashflow

    st.markdown(f"**Total Net Capital Invested (Cash Inflows - Outflows):** ₹{total_invested_cash:,.2f}")

    if not cf_data:
        st.info("No cash flows recorded yet.")
        return

    # --- Pagination (newest cash flows first) ---
    cf_data_desc = list(reversed(cf_data))
    total_records = len(cf_data_desc)

    pg_col1, pg_col2, pg_col3 = st.columns([1, 1, 2])
    with pg_col1:
        page_size_sel = st.selectbox("Rows per page", [25, 50, 100, "All"], index=0, key=f"cf_page_size_{strat_ledger.id}")
    page_size = total_records if page_size_sel == "All" else int(page_size_sel)
    total_pages = max(1, (total_records + page_size - 1) // page_size)
    with pg_col2:
        page = st.selectbox("Page", list(range(1, total_pages + 1)), index=0, key=f"cf_page_{strat_ledger.id}")

    start = (page - 1) * page_size
    end = min(start + page_size, total_records)
    page_rows = cf_data_desc[start:end]
    page_ids = {r['_id'] for r in page_rows if r['_id'] is not None}
    with pg_col3:
        st.caption(f"Showing {start + 1}–{end} of {total_records} cash flows (newest first)")

    cf_df = pd.DataFrame(page_rows)

    cf_columns_config = {
        '_id': None,  # Hide
        'Date': st.column_config.DatetimeColumn("Date", required=True),
        'Amount': st.column_config.NumberColumn("Amount", required=True, step=0.01),
        'Type': st.column_config.SelectboxColumn("Type", options=["INITIAL", "SIP", "SWP", "RESIDUAL"], required=True)
    }

    cf_styled = cf_df.style.map(_color_amount, subset=['Amount'])

    edited_cfs = st.data_editor(
        cf_styled,
        column_config=cf_columns_config,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        key=f"cf_editor_{strat_ledger.id}_{page}_{page_size}"
    )
    st.caption("⚠️ Save changes before switching pages — edits made on a page are only persisted when you save that page.")

    if st.button("Save Cash Flow Changes", type="primary", key="save_cf"):
        # Keep cash flows NOT on the current page, then re-insert the edited page,
        # so paginating never drops the rows you can't currently see.
        kept = [r for r in cf_data if r['_id'] not in page_ids]

        db.query(CashFlow).filter(CashFlow.strategy_id == strat_ledger.id).delete()

        # Re-insert off-page cash flows unchanged
        for r in kept:
            db.add(CashFlow(
                strategy_id=strat_ledger.id,
                date=r['Date'],
                amount=float(r['Amount']),
                flow_type=r['Type']
            ))

        # Re-insert the (possibly edited) current page rows
        for _, row in edited_cfs.iterrows():
            if pd.isna(row['Date']) or pd.isna(row['Amount']):
                continue  # skip blank/incomplete rows added in the editor
            db.add(CashFlow(
                strategy_id=strat_ledger.id,
                date=row['Date'],
                amount=float(row['Amount']),
                flow_type=row['Type']
            ))

        db.commit()
        st.success("Cash Flows updated successfully!")
        st.rerun()


def _build_trade_rows(trades, strat_ledger):
    """Build display rows (newest-relevant fields) with running realized PnL.

    `trades` must be ordered ascending so the weighted-average cost basis is correct.
    """
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
            'Realized PnL': 0.0,
            'Charges': t.charges or 0.0,
            '_charges_breakdown': t.charges_breakdown,
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

    return trade_data


def _render_trade_ledger(db, strat_ledger):
    st.subheader("Trade Ledger")
    trades = db.query(Trade).filter(Trade.strategy_id == strat_ledger.id).order_by(Trade.date.asc()).all()

    # Realized PnL is computed in chronological order over the full history.
    trade_data = _build_trade_rows(trades, strat_ledger)

    if not trade_data:
        st.info("No trades found. Start by executing a batch or logging an override.")
        return

    total_trade_value = sum(t['Total Value'] for t in trade_data)
    total_realized_pnl = sum(t['Realized PnL'] for t in trade_data)
    total_charges = sum(t['Charges'] for t in trade_data)

    st.markdown(
        f"**Gross Traded Value:** ₹{total_trade_value:,.2f} &nbsp;&nbsp;|&nbsp;&nbsp; "
        f"**Total Realized PnL (All Time):** ₹{total_realized_pnl:,.2f} &nbsp;&nbsp;|&nbsp;&nbsp; "
        f"**Total Charges:** ₹{total_charges:,.2f}"
    )

    # --- Pagination (newest trades first) ---
    # Reverse so the latest trades appear on top of the first page.
    trade_data_desc = list(reversed(trade_data))
    total_records = len(trade_data_desc)

    pg_col1, pg_col2, pg_col3 = st.columns([1, 1, 2])
    with pg_col1:
        page_size_sel = st.selectbox("Rows per page", [25, 50, 100, "All"], index=0, key=f"trade_page_size_{strat_ledger.id}")
    page_size = total_records if page_size_sel == "All" else int(page_size_sel)
    total_pages = max(1, (total_records + page_size - 1) // page_size)
    with pg_col2:
        page = st.selectbox("Page", list(range(1, total_pages + 1)), index=0, key=f"trade_page_{strat_ledger.id}")

    start = (page - 1) * page_size
    end = min(start + page_size, total_records)
    page_rows = trade_data_desc[start:end]
    page_ids = {r['_id'] for r in page_rows if r['_id'] is not None}
    with pg_col3:
        st.caption(f"Showing {start + 1}–{end} of {total_records} trades (newest first)")

    page_df = pd.DataFrame(page_rows)

    # Hide internal columns and make certain columns editable
    columns_config = {
        '_id': None,  # Hide
        '_internal_asset': None,  # Hide
        'Date': st.column_config.DatetimeColumn("Date", required=True),
        'Asset': st.column_config.SelectboxColumn("Asset", options=[strat_ledger.asset1, strat_ledger.asset2], required=True),
        'Action': st.column_config.SelectboxColumn("Action", options=["🟢 BUY", "🔴 SELL"], required=True),
        'Units': st.column_config.NumberColumn("Units", min_value=0.0, step=0.01, required=True),
        'Price': st.column_config.NumberColumn("Price", min_value=0.0, step=0.01, required=True),
        'Total Value': st.column_config.NumberColumn("Total Value", disabled=True, format="%.2f"),
        'Realized PnL': st.column_config.NumberColumn("Realized PnL", disabled=True, format="%.2f"),
        'Charges': st.column_config.NumberColumn("Charges", disabled=True, format="%.2f"),
        '_charges_breakdown': None,  # Hide
    }

    def color_pnl(val):
        if pd.isna(val) or val == 0:
            return ''
        return 'color: #00FF00' if val > 0 else 'color: #FF0000'

    styled_df = page_df.style.map(color_pnl, subset=['Realized PnL'])

    edited_trades = st.data_editor(
        styled_df,
        column_config=columns_config,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        key=f"trade_editor_{strat_ledger.id}_{page}_{page_size}"
    )
    st.caption("⚠️ Save changes before switching pages — edits made on a page are only persisted when you save that page.")

    if st.button("Save Ledger Changes & Recalculate Portfolio", type="primary"):
        # Keep trades that are NOT on the current page, then re-insert the edited
        # page, so paginating never drops the rows you can't currently see.
        kept = [r for r in trade_data if r['_id'] not in page_ids]

        db.query(Trade).filter(Trade.strategy_id == strat_ledger.id).delete()

        # Re-insert off-page trades unchanged (charges preserved verbatim)
        for r in kept:
            db.add(Trade(
                strategy_id=strat_ledger.id,
                date=r['Date'],
                asset=r['_internal_asset'],
                trade_type='BUY' if 'BUY' in r['Action'] else 'SELL',
                units=float(r['Units']),
                price=float(r['Price']),
                charges=r['Charges'],
                charges_breakdown=r['_charges_breakdown']
            ))

        # Original page rows by id, so we can preserve charges (incl. pledge) on
        # rows that weren't actually changed, and recompute only edited/new ones.
        orig_by_id = {r['_id']: r for r in page_rows}

        # Re-insert the (possibly edited) current page rows
        for _, row in edited_trades.iterrows():
            if pd.isna(row['Date']) or pd.isna(row['Units']) or pd.isna(row['Price']):
                continue  # skip blank/incomplete rows added in the editor
            asset_code = 'ASSET1' if row['Asset'] == strat_ledger.asset1 else 'ASSET2'
            action_code = 'BUY' if 'BUY' in row['Action'] else 'SELL'
            units = float(row['Units'])
            price = float(row['Price'])

            rid = None if pd.isna(row['_id']) else int(row['_id'])
            orig = orig_by_id.get(rid) if rid is not None else None
            unchanged = (
                orig is not None
                and orig['_internal_asset'] == asset_code
                and orig['Action'] == row['Action']
                and abs(orig['Units'] - units) < 1e-9
                and abs(orig['Price'] - price) < 1e-9
            )
            if unchanged:
                charges, breakdown = orig['Charges'], orig['_charges_breakdown']
            else:
                ticker = strat_ledger.asset1 if asset_code == 'ASSET1' else strat_ledger.asset2
                charges, breakdown = compute_trade_charges(ticker, action_code, units, price)

            db.add(Trade(
                strategy_id=strat_ledger.id,
                date=row['Date'],
                asset=asset_code,
                trade_type=action_code,
                units=units,
                price=price,
                charges=charges,
                charges_breakdown=breakdown
            ))

        db.commit()
        recalculate_portfolio_from_ledger(db, strat_ledger.id)

        st.success("Ledger updated successfully! Live portfolio exactly matches your trade history.")
        st.rerun()
