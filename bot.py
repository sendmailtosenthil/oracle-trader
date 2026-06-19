import schedule
import time
import datetime
import pytz
import smtplib
import os
from email.message import EmailMessage

from database import SessionLocal, Strategy, PendingSwitch, Portfolio, CashFlow, init_db
from donchian import evaluate_donchian_intraday

IST = pytz.timezone('Asia/Kolkata')

def send_email(html_content, subject):
    sender_email = os.environ.get('GMAIL_USER')
    sender_password = os.environ.get('GMAIL_PASS')
    receiver_email = 'sendmailtosenthil@gmail.com'
    
    if not sender_email or not sender_password:
        print("Email credentials missing. Please set GMAIL_USER and GMAIL_PASS.")
        return
        
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = sender_email
    msg['To'] = receiver_email
    msg.set_content("Please enable HTML to view this report.")
    msg.add_alternative(html_content, subtype='html')
    
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(sender_email, sender_password)
            smtp.send_message(msg)
        print(f"Sent email: {subject}")
    except Exception as e:
        print(f"Failed to send email: {e}")

def check_intraday_signals():
    now_ist = datetime.datetime.now(IST)
    if now_ist.weekday() >= 5:
        return
        
    # Only run End-of-Day
    # Time checking is now handled by the schedule library naturally
        
    print(f"[{now_ist.strftime('%H:%M:%S')}] Running intraday check...")
    
    db = SessionLocal()
    strategies = db.query(Strategy).all()
    
    for strat in strategies:
        result = evaluate_donchian_intraday(strat.asset1, strat.asset2, strat.window)
        if not result:
            continue
            
        signal = result['signal']
        
        # If there's a signal and it's different from our currently targeted signal
        if signal and signal != strat.current_signal_target:
            print(f"🚨 SIGNAL TRIGGERED FOR {strat.name}: Switch to {signal}!")
            
            # Check if there is already a pending switch
            existing_pending = db.query(PendingSwitch).filter(
                PendingSwitch.strategy_id == strat.id,
                PendingSwitch.status == 'PENDING'
            ).first()
            
            if not existing_pending:
                from_asset = 'ASSET1' if signal == 'ASSET2' else 'ASSET2'
                to_asset = signal
                
                # Snapshot the current portfolio units we need to sell
                port = db.query(Portfolio).filter(
                    Portfolio.strategy_id == strat.id,
                    Portfolio.asset == from_asset
                ).first()
                
                units_to_sell = port.units if port else 0.0
                
                new_switch = PendingSwitch(
                    strategy_id=strat.id,
                    from_asset=from_asset,
                    to_asset=to_asset,
                    total_units_to_sell=units_to_sell,
                    units_sold_so_far=0.0,
                    status='PENDING'
                )
                db.add(new_switch)
                
                # Update the target state
                strat.current_signal_target = signal
                db.commit()
                
                target_ticker = strat.asset1 if signal == 'ASSET1' else strat.asset2
                
                # Send Alert Email
                html = f"""
                <h2 style="color: red;">🚨 URGENT TREND REVERSAL 🚨</h2>
                <p>The Donchian {strat.window}-Day Channel for <b>{strat.name}</b> has been broken!</p>
                <h3>Action Required: SWITCH TO {target_ticker}</h3>
                <p>Please log into your Project Oracle Dashboard to execute the batch switch.</p>
                <ul>
                    <li>Live Ratio: {result['live_ratio']:.4f}</li>
                    <li>Upper Limit: {result['upper']:.4f}</li>
                    <li>Lower Limit: {result['lower']:.4f}</li>
                </ul>
                """
                send_email(html, f"🚨 ORACLE ALERT: Switch {strat.name} to {target_ticker}")
            
    db.close()

def send_daily_summary():
    print("Generating daily 8:30 AM summary...")
    db = SessionLocal()
    strategies = db.query(Strategy).all()
    
    html = f"<h2>Daily Portfolio Summary ({datetime.datetime.now(IST).strftime('%Y-%m-%d')})</h2>"
    
    for strat in strategies:
        portfolios = db.query(Portfolio).filter(Portfolio.strategy_id == strat.id).all()
        
        asset1_port = next((p for p in portfolios if p.asset == 'ASSET1'), None)
        asset2_port = next((p for p in portfolios if p.asset == 'ASSET2'), None)
        
        # Get latest prices
        res = evaluate_donchian_intraday(strat.asset1, strat.asset2, strat.window)
        if not res:
            continue
            
        val1 = (asset1_port.units * res['live_price1']) if asset1_port else 0
        val2 = (asset2_port.units * res['live_price2']) if asset2_port else 0
        total_val = val1 + val2
        
        invested = sum([p.invested_amount for p in portfolios])
        roi = ((total_val / invested) - 1) * 100 if invested > 0 else 0
        
        pending = db.query(PendingSwitch).filter(PendingSwitch.strategy_id == strat.id, PendingSwitch.status == 'PENDING').first()
        status_text = "<span style='color:red; font-weight:bold;'>PENDING BATCH SWITCH</span>" if pending else "<span style='color:green;'>All good, holding steady.</span>"
        
        html += f"""
        <div style="border: 1px solid #ddd; padding: 15px; margin-bottom: 20px;">
            <h3 style="margin-top: 0;">{strat.name}</h3>
            <p>Status: {status_text}</p>
            <table style="width: 100%; text-align: left;">
                <tr><th>{strat.asset1} Units</th><td>{asset1_port.units:.2f}</td></tr>
                <tr><th>{strat.asset2} Units</th><td>{asset2_port.units:.2f}</td></tr>
                <tr><th>Total Invested</th><td>₹{invested:,.2f}</td></tr>
                <tr><th>Current Value</th><td><b>₹{total_val:,.2f}</b></td></tr>
                <tr><th>Overall ROI</th><td style="color: {'green' if roi >= 0 else 'red'};">{roi:.2f}%</td></tr>
            </table>
        </div>
        """
        
    db.close()
    send_email(html, "Project Oracle: Daily Portfolio State")


def run_bot():
    print("Starting Oracle Bot Daemon...")
    init_db() # CRITICAL: Must initialize database to bind SessionLocal before using it!
    
    # End of Day Scan at 3:35 PM IST
    schedule.every().day.at("15:35", "Asia/Kolkata").do(check_intraday_signals)
    
    # Daily Morning Email
    # Passing the timezone explicitly so it always triggers at 8:30 AM IST regardless of the VPS OS clock
    schedule.every().day.at("08:30", "Asia/Kolkata").do(send_daily_summary)
    
    # Run once on startup just to verify
    check_intraday_signals()
    
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    run_bot()
