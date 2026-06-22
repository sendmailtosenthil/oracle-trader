"""Portfolio finance calculations (XIRR, realized PnL) — no UI dependencies."""
import datetime

from pyxirr import xirr

from bees.database import CashFlow


def calculate_xirr(db, strategy_id, current_value):
    """Annualized XIRR (%) for a strategy, treating current_value as a final inflow."""
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
    except Exception:
        return 0.0


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
