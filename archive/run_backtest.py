import yfinance as yf
import pandas as pd
import numpy as np
import datetime

def run_backtest():
    print("Downloading 10-year data for NIFTYBEES.NS and GOLDBEES.NS...")
    
    # Download 10 years of data
    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=365*10)
    
    nifty = yf.download('NIFTYBEES.NS', start=start_date, end=end_date, progress=False)['Close']
    gold = yf.download('GOLDBEES.NS', start=start_date, end=end_date, progress=False)['Close']
    
    # Handle yfinance returning DataFrames for single columns in newer versions
    if isinstance(nifty, pd.DataFrame):
        nifty = nifty.iloc[:, 0]
    if isinstance(gold, pd.DataFrame):
        gold = gold.iloc[:, 0]
        
    df = pd.DataFrame({'NIFTY': nifty, 'GOLD': gold}).dropna()
    
    if df.empty:
        print("Error: No data downloaded.")
        return

    print(f"Data ready from {df.index[0].date()} to {df.index[-1].date()} ({len(df)} trading days)")

    # Strategy Parameters
    WINDOW = 25
    INITIAL_CAPITAL = 300000.0
    WEEKLY_SIP_TOTAL = 5000.0  # 2.5k per instrument = 5k total
    
    # Calculate Donchian Channel on Ratio
    df['Ratio'] = df['NIFTY'] / df['GOLD']
    df['Upper'] = df['Ratio'].rolling(window=WINDOW).max().shift(1)
    df['Lower'] = df['Ratio'].rolling(window=WINDOW).min().shift(1)
    
    # Drop NaNs from the rolling window
    df = df.dropna()
    
    # State tracking
    current_asset = None # Will be determined on first day
    units = 0.0
    cash = INITIAL_CAPITAL
    total_invested = INITIAL_CAPITAL
    
    last_week = -1
    
    history = []
    
    for i in range(len(df)):
        date = df.index[i]
        price_nifty = float(df['NIFTY'].iloc[i])
        price_gold = float(df['GOLD'].iloc[i])
        ratio = float(df['Ratio'].iloc[i])
        upper = float(df['Upper'].iloc[i])
        lower = float(df['Lower'].iloc[i])
        
        current_week = date.isocalendar()[1]
        
        # Weekly SIP Injection on the first trading day of a new week
        if current_week != last_week and i > 0:
            cash += WEEKLY_SIP_TOTAL
            total_invested += WEEKLY_SIP_TOTAL
            last_week = current_week
        elif last_week == -1:
            last_week = current_week
            
        # Determine Signal based on Donchian Breakout
        signal = current_asset
        if ratio > upper:
            signal = 'NIFTY'
        elif ratio < lower:
            signal = 'GOLD'
            
        # Initial purchase on day 1
        if current_asset is None:
            current_asset = signal if signal else 'NIFTY' # Default to NIFTY if no signal
            if current_asset == 'NIFTY':
                units = cash / price_nifty
            else:
                units = cash / price_gold
            cash = 0.0
            
        # Process Switch or Reinvest Cash
        if signal and signal != current_asset:
            # Sell old
            if current_asset == 'NIFTY':
                cash += units * price_nifty
            else:
                cash += units * price_gold
            
            # Switch asset
            current_asset = signal
            
            # Buy new
            if current_asset == 'NIFTY':
                units = cash / price_nifty
            else:
                units = cash / price_gold
            cash = 0.0
            
        # If we just had an SIP and didn't switch, reinvest the cash into current asset
        if cash > 0:
            if current_asset == 'NIFTY':
                units += cash / price_nifty
            else:
                units += cash / price_gold
            cash = 0.0
            
        # Record daily portfolio value
        val_nifty = units * price_nifty if current_asset == 'NIFTY' else 0.0
        val_gold = units * price_gold if current_asset == 'GOLD' else 0.0
        total_val = val_nifty + val_gold + cash
        
        history.append({
            'Date': date,
            'Asset': current_asset,
            'Total Value': total_val,
            'Invested': total_invested
        })
        
    res_df = pd.DataFrame(history)
    final_val = res_df['Total Value'].iloc[-1]
    final_invested = res_df['Invested'].iloc[-1]
    
    net_profit = final_val - final_invested
    absolute_roi = (net_profit / final_invested) * 100
    
    years = (df.index[-1] - df.index[0]).days / 365.25
    cagr = ((final_val / INITIAL_CAPITAL) ** (1 / years) - 1) * 100 # Rough CAGR without SIP weightings, XIRR is better
    
    print("\n" + "="*50)
    print("BACKTEST RESULTS (10 Years: Donchian 25-Day)")
    print("="*50)
    print(f"Start Date:          {df.index[0].date()}")
    print(f"End Date:            {df.index[-1].date()}")
    print(f"Initial Capital:     ₹{INITIAL_CAPITAL:,.2f}")
    print(f"Weekly SIP:          ₹{WEEKLY_SIP_TOTAL:,.2f}")
    print(f"Total Invested:      ₹{final_invested:,.2f}")
    print("-" * 50)
    print(f"Final Value:         ₹{final_val:,.2f}")
    print(f"Net Profit:          ₹{net_profit:,.2f}")
    print(f"Absolute ROI:        {absolute_roi:.2f}%")
    
    # Calculate simple benchmark (holding 50/50 from start + splitting SIP 50/50)
    print("-" * 50)
    print("BENCHMARK (Buy & Hold 50/50 Split):")
    b_nifty_units = (INITIAL_CAPITAL / 2) / float(df['NIFTY'].iloc[0])
    b_gold_units = (INITIAL_CAPITAL / 2) / float(df['GOLD'].iloc[0])
    
    b_last_week = -1
    for i in range(len(df)):
        date = df.index[i]
        b_current_week = date.isocalendar()[1]
        
        if b_current_week != b_last_week and i > 0:
            b_nifty_units += (WEEKLY_SIP_TOTAL / 2) / float(df['NIFTY'].iloc[i])
            b_gold_units += (WEEKLY_SIP_TOTAL / 2) / float(df['GOLD'].iloc[i])
            b_last_week = b_current_week
        elif b_last_week == -1:
            b_last_week = b_current_week
            
    bench_val = (b_nifty_units * float(df['NIFTY'].iloc[-1])) + (b_gold_units * float(df['GOLD'].iloc[-1]))
    bench_roi = ((bench_val - final_invested) / final_invested) * 100
    
    print(f"Benchmark Value:     ₹{bench_val:,.2f}")
    print(f"Benchmark ROI:       {bench_roi:.2f}%")
    print(f"Strategy Alpha:      {absolute_roi - bench_roi:+.2f}%")
    print("="*50)

if __name__ == "__main__":
    run_backtest()
