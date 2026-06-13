import yfinance as yf
import pytz
import datetime
import pandas as pd

def get_asset_metrics(ticker):
    df = yf.download(ticker, period='5d', progress=False)
    if df.empty: return 0.0, 0.0
    
    close_series = df['Close']
    if isinstance(close_series, pd.DataFrame):
        close_series = close_series[ticker]
        
    print(f"Original close_series for {ticker}:\n{close_series}")
    close_series = close_series.dropna()
    print(f"After dropna for {ticker}:\n{close_series}")
        
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.datetime.now(ist)
    
    if now.hour < 15 or (now.hour == 15 and now.minute < 30):
        today_str = now.strftime('%Y-%m-%d')
        df_filtered = close_series[close_series.index.strftime('%Y-%m-%d') != today_str]
        print(f"Filtered for {ticker}:\n{df_filtered}")
        if len(df_filtered) >= 2:
            return float(df_filtered.iloc[-1]), float(df_filtered.iloc[-2])
        elif len(df_filtered) == 1:
            return float(df_filtered.iloc[-1]), float(df_filtered.iloc[-1])
        return 0.0, 0.0
    else:
        if len(close_series) >= 2:
            return float(close_series.iloc[-1]), float(close_series.iloc[-2])
        elif len(close_series) == 1:
            return float(close_series.iloc[-1]), float(close_series.iloc[-1])
        return 0.0, 0.0

print(get_asset_metrics("NIFTYBEES.NS"))
print(get_asset_metrics("GOLDBEES.NS"))
