import yfinance as yf
import pandas as pd
import datetime
import pytz

def get_latest_intraday_data(ticker):
    df = yf.download(ticker, period='5d', interval='1m', progress=False)
    if df.empty:
        return None, None
    close = df['Close'] if 'Close' in df else df.iloc[:, 3]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close.dropna()
    if close.empty:
        return None, None
    
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.datetime.now(ist)
    
    if now.hour < 15 or (now.hour == 15 and now.minute < 30):
        today_str = now.strftime('%Y-%m-%d')
        close = close[close.index.strftime('%Y-%m-%d') != today_str]
        if close.empty:
            return None, None
            
    last_dt = close.index[-1]
    return float(close.iloc[-1]), pd.to_datetime(last_dt.strftime('%Y-%m-%d'))

print("NIFTY:", get_latest_intraday_data("NIFTYBEES.NS"))
print("GOLD:", get_latest_intraday_data("GOLDBEES.NS"))
