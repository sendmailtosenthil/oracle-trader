import yfinance as yf
import pandas as pd
import datetime

def clean_indian_etf_data(series):
    adj = series.copy()
    for i in range(1, len(adj) - 5):
        prev = adj.iloc[i-1]
        curr = adj.iloc[i]
        if curr < prev * 0.5:
            if adj.iloc[i:i+5].max() > prev * 0.8:
                adj.iloc[i] = prev
                for j in range(i+1, i+5):
                    if adj.iloc[j] < prev * 0.5:
                        adj.iloc[j] = prev
                    else:
                        break
    for i in range(1, len(adj)):
        prev = adj.iloc[i-1]
        curr = adj.iloc[i]
        if curr < prev * 0.5:
            ratio = round(prev / curr)
            if ratio >= 2:
                adj.iloc[:i] = adj.iloc[:i] / ratio
    return adj

def get_clean_daily_close(ticker, window):
    end_date_dt = datetime.datetime.today()
    start_date_dt = end_date_dt - datetime.timedelta(days=window * 2 + 30)
    
    end_date = end_date_dt.strftime("%Y-%m-%d")
    start_date = start_date_dt.strftime("%Y-%m-%d")
    
    df = yf.download(ticker, start=start_date, end=end_date, progress=False)
    close = df['Close'] if 'Close' in df else df.iloc[:, 3]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close[close > 0].dropna()
    return clean_indian_etf_data(close)

def get_latest_intraday_data(ticker):
    # Fetch 5 days of 1-minute data to safely cover long weekends
    df = yf.download(ticker, period='5d', interval='1m', progress=False)
    if df.empty:
        return None, None
    close = df['Close'] if 'Close' in df else df.iloc[:, 3]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close.dropna()
    if close.empty:
        return None, None
        
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.datetime.now(ist)
    
    if now.hour < 15 or (now.hour == 15 and now.minute < 30):
        # Before 3:30 PM, exclude today's date from the dataset
        today_str = now.strftime('%Y-%m-%d')
        close = close[close.index.strftime('%Y-%m-%d') != today_str]
        if close.empty:
            return None, None
            
    last_dt = close.index[-1]
    return float(close.iloc[-1]), pd.to_datetime(last_dt.strftime('%Y-%m-%d'))

def evaluate_donchian_intraday(asset1, asset2, window):
    adj1 = get_clean_daily_close(asset1, window)
    adj2 = get_clean_daily_close(asset2, window)
    
    df = pd.DataFrame({'ASSET1': adj1, 'ASSET2': adj2}).dropna()
    if df.empty:
        return None
        
    # Get the targeted prices based on the 3:30 PM rule
    price1, date1 = get_latest_intraday_data(asset1)
    price2, date2 = get_latest_intraday_data(asset2)
    
    # Append to historical dataframe accurately
    if price1 and price2 and date1 == date2:
        df.loc[date1, 'ASSET1'] = price1
        df.loc[date1, 'ASSET2'] = price2
        
    if date1:
        # Strip out any buggy future dates yfinance might have injected
        df = df[df.index <= date1]
        
    df['Ratio'] = df['ASSET1'] / df['ASSET2']
    # Calculate historical upper/lower based on previous days
    df['Upper'] = df['Ratio'].shift(1).rolling(window=window).max()
    df['Lower'] = df['Ratio'].shift(1).rolling(window=window).min()
    
    upper = df['Upper'].iloc[-1]
    lower = df['Lower'].iloc[-1]
    
    live_price1 = df['ASSET1'].iloc[-1]
    live_price2 = df['ASSET2'].iloc[-1]
    live_ratio = df['Ratio'].iloc[-1]
    
    signal = None
    if live_ratio > upper:
        signal = 'ASSET1'
    elif live_ratio < lower:
        signal = 'ASSET2'
        
    return {
        'live_price1': live_price1,
        'live_price2': live_price2,
        'live_ratio': live_ratio,
        'upper': upper,
        'lower': lower,
        'signal': signal,
        'df': df # useful for plotting
    }
