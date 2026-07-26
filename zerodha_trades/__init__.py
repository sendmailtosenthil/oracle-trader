"""Zerodha Trades — group live Kite positions and manage them as risk units.

Zerodha reports a multi-leg structure as unrelated positions. This module lets
the user tag positions (or slices of them) into named *groups*, give each group
its own rupee stoploss / target, and monitor the group's combined P&L.
"""
