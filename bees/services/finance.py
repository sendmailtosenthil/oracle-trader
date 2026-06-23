"""Portfolio finance calculations (XIRR, realized PnL) — no UI dependencies."""
import datetime

from pyxirr import xirr

from common.database import CashFlow, Trade


def total_charges(db, strategy_id):
    """Sum of all trading charges incurred for a strategy."""
    trades = db.query(Trade).filter(Trade.strategy_id == strategy_id).all()
    return sum((t.charges or 0.0) for t in trades)


def calculate_xirr(db, strategy_id, current_value):
    """Annualized XIRR (%) for a strategy, treating current_value as a final inflow.

    Trading charges are folded in as dated cash outflows (money that left the
    account), so the XIRR reflects the true, after-cost return.
    """
    cash_flows = db.query(CashFlow).filter(CashFlow.strategy_id == strategy_id).all()
    if not cash_flows:
        return 0.0

    dates = [cf.date.date() for cf in cash_flows]
    amounts = [cf.amount for cf in cash_flows]

    # Each trade's charges are a real cash outflow on its trade date.
    trades = db.query(Trade).filter(Trade.strategy_id == strategy_id).all()
    for t in trades:
        if t.charges:
            dates.append(t.date.date())
            amounts.append(-t.charges)

    dates.append(datetime.datetime.today().date())
    amounts.append(current_value)

    try:
        calculated = xirr(dates, amounts)
        return (calculated * 100) if calculated else 0.0
    except Exception:
        return 0.0


def realized_charges_by_asset(trades):
    """Sum charges per asset code (charges are attached to the trade's asset)."""
    by_asset = {'ASSET1': 0.0, 'ASSET2': 0.0}
    for t in trades:
        if t.asset in by_asset:
            by_asset[t.asset] += (t.charges or 0.0)
    return by_asset


def realized_pnl_by_asset(trades):
    """Walk trades chronologically and return realized PnL per asset code.

    `trades` must be ordered by date ascending. Uses weighted-average cost basis.
    """
    realized = {'ASSET1': 0.0, 'ASSET2': 0.0}
    avg_price = {'ASSET1': 0.0, 'ASSET2': 0.0}
    units_held = {'ASSET1': 0.0, 'ASSET2': 0.0}

    for t in trades:
        asset = t.asset
        if t.trade_type == 'BUY':
            cost = t.units * t.price
            new_u = units_held[asset] + t.units
            if new_u > 0:
                avg_price[asset] = ((units_held[asset] * avg_price[asset]) + cost) / new_u
            units_held[asset] = new_u
        elif t.trade_type == 'SELL':
            realized[asset] += (t.price - avg_price[asset]) * t.units
            units_held[asset] -= t.units

    return realized
