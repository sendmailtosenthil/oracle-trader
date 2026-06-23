"""momentum — Nifty500 weighted-momentum strategy module.

Ranks the Nifty500 universe by a factor-blended, volatility-adjusted momentum
score (ported from the quant-momentum backtester), tracks a 15-stock equal-weight
portfolio, and recommends rebalances (sell laggards whose rank drops past a
threshold, redeploy into the best-ranked unheld names).

Daily OHLC price history lives in ``data/momentum/cache`` (one JSON per symbol)
and point-in-time index membership in ``data/momentum/constituents``. Prices can
be refreshed from Zerodha via the shared ``common`` Kite client. UI lives in
``momentum.views``.
"""
