"""Market-data helpers backed by yfinance.

All times are evaluated in IST; before the 3:30 PM market close the current
day's (incomplete) candle is excluded so figures reflect the last closed day.
"""
import datetime

import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone('Asia/Kolkata')


def _before_market_close(now=None):
    now = now or datetime.datetime.now(IST)
    return now.hour < 15 or (now.hour == 15 and now.minute < 30)


def get_reference_close(ticker):
    """Latest closed daily close for a ticker (today excluded before 3:30 PM IST)."""
    df = yf.download(ticker, period='5d', progress=False)
    if df.empty:
        return 0.0

    now = datetime.datetime.now(IST)
    if _before_market_close(now):
        today_str = now.strftime('%Y-%m-%d')
        df = df[df.index.strftime('%Y-%m-%d') != today_str]

    if df.empty:
        return 0.0
    close_series = df['Close']
    if isinstance(close_series, pd.DataFrame):
        close_series = close_series[ticker]
    close_series = close_series.dropna()
    return float(close_series.iloc[-1])


def get_asset_metrics(ticker):
    """Return (latest_close, previous_close) from 1-minute data resampled to daily."""
    df = yf.download(ticker, period='5d', interval='1m', progress=False)
    if df.empty:
        return 0.0, 0.0

    close = df['Close'] if 'Close' in df else df.iloc[:, 3]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close.dropna()

    if close.empty:
        return 0.0, 0.0

    if close.index.tz is None:
        close.index = close.index.tz_localize('Asia/Kolkata')
    else:
        close.index = close.index.tz_convert('Asia/Kolkata')

    daily_close = close.resample('D').last().dropna()

    now = datetime.datetime.now(IST)
    if _before_market_close(now):
        today_str = now.strftime('%Y-%m-%d')
        daily_close = daily_close[daily_close.index.strftime('%Y-%m-%d') != today_str]

    if len(daily_close) >= 2:
        return float(daily_close.iloc[-1]), float(daily_close.iloc[-2])
    elif len(daily_close) == 1:
        return float(daily_close.iloc[-1]), float(daily_close.iloc[-1])

    return 0.0, 0.0
