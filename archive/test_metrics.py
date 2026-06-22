import yfinance as yf
import pytz
import datetime
import pandas as pd

def get_asset_metrics(ticker):
    df = yf.download(ticker, period='5d', interval='1m', progress=False)
    if df.empty: return 0.0, 0.0
    
    close = df['Close'] if 'Close' in df else df.iloc[:, 3]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close.dropna()
    
    if close.empty: return 0.0, 0.0
    
    # yfinance 1m data usually has localized datetime or naive. 
    # Let's check its tz
    if close.index.tz is None:
        close.index = close.index.tz_localize('Asia/Kolkata')
    else:
        close.index = close.index.tz_convert('Asia/Kolkata')
        
    daily_close = close.resample('D').last().dropna()
    print(f"Daily Close for {ticker}:")
    print(daily_close)
    
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.datetime.now(ist)
    
    if now.hour < 15 or (now.hour == 15 and now.minute < 30):
        today_str = now.strftime('%Y-%m-%d')
        daily_close = daily_close[daily_close.index.strftime('%Y-%m-%d') != today_str]
        
    if len(daily_close) >= 2:
        return float(daily_close.iloc[-1]), float(daily_close.iloc[-2])
    elif len(daily_close) == 1:
        return float(daily_close.iloc[-1]), float(daily_close.iloc[-1])
        
    return 0.0, 0.0

print("NIFTY:", get_asset_metrics("NIFTYBEES.NS"))
print("GOLD:", get_asset_metrics("GOLDBEES.NS"))
