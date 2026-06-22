"""Zerodha (NSE, delivery/ETF) trading-charge calculator + reconciliation.

Rates are module-level constants so they can be tuned against an actual Zerodha
contract note. STT is ETF-category specific (equity ETF vs gold ETF).

Two charges are NOT pure per-trade and are assigned by reconcile_strategy_charges
over the full ledger:
  - DP charges: Rs.13.5 + GST, levied ONCE per instrument per day on the SELL
    side (attached to that day's last sell of the instrument), exactly as Zerodha.
  - Pledge request: Rs.30 + GST, levied ONCE per completed full switch, attached
    to the last BUY that completes the switch (a switch may span a day or two).

Stamp duty on securities is centrally unified (since 1 Jul 2020): 0.015% on the
buy side, the same in every Indian state — so it is NOT state-dependent.

Per-trade base components (equity delivery / ETF, NSE):
  - Brokerage:            0 (Zerodha, delivery)
  - STT:                  ETF-category & side specific (see STT_RATES)
  - Exchange txn (NSE):   0.00297% of turnover, both sides
  - SEBI turnover fee:    Rs.10 / crore (0.0001%), both sides
  - Stamp duty:           0.015% of turnover, BUY only
  - GST:                  18% on (brokerage + exchange txn + SEBI)
"""
import json
from collections import defaultdict

# --- Rates (edit to match your contract note) ---
BROKERAGE = 0.0
EXCHANGE_TXN_RATE = 0.0000297      # 0.00297% (NSE cash/ETF)
SEBI_RATE = 0.000001               # Rs.10 per crore = 0.0001%
STAMP_DUTY_RATE_BUY = 0.00015      # 0.015% (central, buy only)
GST_RATE = 0.18                    # 18%
DP_CHARGE_BASE = 13.5              # + GST, once per instrument per day (sell side)
PLEDGE_CHARGE_BASE = 30.0          # + GST, once per completed switch (last buy)

# STT is category-specific. Equity ETFs are taxed like equity-oriented fund
# units (0.001% on sell only); gold ETFs attract no STT. Verify per contract note.
STT_RATES = {
    'equity_etf': {'BUY': 0.0, 'SELL': 0.00001},   # 0.001% on sell only
    'gold_etf':   {'BUY': 0.0, 'SELL': 0.0},        # no STT
}

# Map each tradable ticker to its STT category.
ETF_CATEGORY = {
    'NIFTYBEES.NS': 'equity_etf',
    'BANKBEES.NS': 'equity_etf',
    'ITBEES.NS': 'equity_etf',
    'GOLDBEES.NS': 'gold_etf',
}
DEFAULT_CATEGORY = 'equity_etf'


def _r(x):
    return round(x, 2)


def ticker_for(strategy, asset_code):
    """Resolve an 'ASSET1'/'ASSET2' code to the strategy's actual ticker."""
    return strategy.asset1 if asset_code == 'ASSET1' else strategy.asset2


def category_for(ticker):
    return ETF_CATEGORY.get(ticker, DEFAULT_CATEGORY)


def dp_charge():
    """DP charge (Rs.13.5 + GST), once per instrument per day on the sell side."""
    return _r(DP_CHARGE_BASE * (1 + GST_RATE))


def pledge_charge():
    """Pledge request charge (Rs.30 + GST), once per completed switch."""
    return _r(PLEDGE_CHARGE_BASE * (1 + GST_RATE))


def compute_base_charges(ticker, trade_type, units, price):
    """Per-trade base charges (STT, exchange txn, SEBI, stamp duty, GST).

    Excludes DP and pledge, which are day/switch-level and assigned by
    reconcile_strategy_charges. Returns (total_base, breakdown_dict).
    """
    side = 'BUY' if trade_type == 'BUY' else 'SELL'
    turnover = float(units) * float(price)
    category = category_for(ticker)

    brokerage = BROKERAGE
    stt = turnover * STT_RATES.get(category, STT_RATES[DEFAULT_CATEGORY])[side]
    exchange_txn = turnover * EXCHANGE_TXN_RATE
    sebi = turnover * SEBI_RATE
    stamp_duty = turnover * STAMP_DUTY_RATE_BUY if side == 'BUY' else 0.0
    gst = GST_RATE * (brokerage + exchange_txn + sebi)

    breakdown = {
        'brokerage': _r(brokerage),
        'stt': _r(stt),
        'exchange_txn': _r(exchange_txn),
        'sebi': _r(sebi),
        'stamp_duty': _r(stamp_duty),
        'gst': _r(gst),
        'dp': 0.0,
        'pledge': 0.0,
    }
    total = _r(sum(breakdown.values()))
    breakdown['total'] = total
    return total, breakdown


def reconcile_strategy_charges(db, strategy_id):
    """Recompute and persist all charges for a strategy from the full ledger.

    Deterministic and idempotent. Base charges per trade, plus:
      - DP on each (instrument, day) group's last SELL,
      - pledge on each trade the user has manually flagged (trade.pledge).
    """
    from bees.database import Strategy, Trade

    strat = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strat:
        return
    trades = (db.query(Trade)
              .filter(Trade.strategy_id == strategy_id)
              .order_by(Trade.date.asc(), Trade.id.asc())
              .all())

    # 1. Base charges per trade (dp/pledge start at 0).
    breakdowns = {}
    for t in trades:
        ticker = ticker_for(strat, t.asset)
        _, bd = compute_base_charges(ticker, t.trade_type, t.units, t.price)
        breakdowns[t.id] = bd

    # 2. DP: one per (instrument, day) on the sell side -> that day's last sell.
    dp = dp_charge()
    sell_groups = defaultdict(list)
    for t in trades:
        if t.trade_type == 'SELL':
            sell_groups[(t.asset, t.date.date())].append(t)
    for group_trades in sell_groups.values():
        last_sell = max(group_trades, key=lambda t: (t.date, t.id))
        bd = breakdowns[last_sell.id]
        bd['dp'] = dp
        bd['total'] = _r(bd['total'] + dp)

    # 3. Pledge: applied to any trade the user has manually flagged (a switch may
    #    take a day or two; the user ticks the final buy that completes it).
    pledge = pledge_charge()
    for t in trades:
        if getattr(t, 'pledge', False):
            bd = breakdowns[t.id]
            bd['pledge'] = _r(bd['pledge'] + pledge)
            bd['total'] = _r(bd['total'] + pledge)

    # 4. Persist.
    for t in trades:
        bd = breakdowns[t.id]
        t.charges = bd['total']
        t.charges_breakdown = json.dumps(bd)
    db.commit()
