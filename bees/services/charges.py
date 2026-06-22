"""Zerodha (NSE, delivery/ETF) trading-charge calculator.

All rates are module-level constants so they can be tuned against an actual
Zerodha contract note. STT is ETF-category specific (equity ETF vs gold ETF).

Stamp duty on securities is centrally unified (since 1 Jul 2020): 0.015% on the
buy side, the same in every Indian state — so it is NOT state-dependent.

Charge components (equity delivery / ETF, NSE):
  - Brokerage:            0 (Zerodha, delivery)
  - STT:                  ETF-category & side specific (see STT_RATES)
  - Exchange txn (NSE):   0.00297% of turnover, both sides
  - SEBI turnover fee:    Rs.10 / crore (0.0001%), both sides
  - Stamp duty:           0.015% of turnover, BUY only
  - GST:                  18% on (brokerage + exchange txn + SEBI)
  - DP charges:           Rs.13.5 + GST, SELL only, per scrip
  - Pledge request:       Rs.30 + GST, applied to the last BUY of a completed
                          full switch (collateral pledge of the new holding)
"""
import json

# --- Rates (edit to match your contract note) ---
BROKERAGE = 0.0
EXCHANGE_TXN_RATE = 0.0000297      # 0.00297% (NSE cash/ETF)
SEBI_RATE = 0.000001               # Rs.10 per crore = 0.0001%
STAMP_DUTY_RATE_BUY = 0.00015      # 0.015% (central, buy only)
GST_RATE = 0.18                    # 18%
DP_CHARGE_BASE = 13.5              # + GST, per sell (per scrip / day)
PLEDGE_CHARGE_BASE = 30.0          # + GST, per pledge request (per ISIN)

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

# Round money to paise.
def _r(x):
    return round(x, 2)


def ticker_for(strategy, asset_code):
    """Resolve an 'ASSET1'/'ASSET2' code to the strategy's actual ticker."""
    return strategy.asset1 if asset_code == 'ASSET1' else strategy.asset2


def category_for(ticker):
    return ETF_CATEGORY.get(ticker, DEFAULT_CATEGORY)


def compute_trade_charges(ticker, trade_type, units, price, include_pledge=False):
    """Return (total_charges, breakdown_json) for a single trade.

    trade_type: 'BUY' or 'SELL'. include_pledge adds the pledge request charge
    (only meaningful on the BUY that completes a full switch).
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
    dp = DP_CHARGE_BASE * (1 + GST_RATE) if side == 'SELL' else 0.0
    pledge = PLEDGE_CHARGE_BASE * (1 + GST_RATE) if (include_pledge and side == 'BUY') else 0.0

    breakdown = {
        'brokerage': _r(brokerage),
        'stt': _r(stt),
        'exchange_txn': _r(exchange_txn),
        'sebi': _r(sebi),
        'stamp_duty': _r(stamp_duty),
        'gst': _r(gst),
        'dp': _r(dp),
        'pledge': _r(pledge),
    }
    total = _r(sum(breakdown.values()))
    breakdown['total'] = total
    return total, json.dumps(breakdown)


def pledge_charge():
    """The pledge request charge (Rs.30 + GST), added once per completed switch."""
    return _r(PLEDGE_CHARGE_BASE * (1 + GST_RATE))
