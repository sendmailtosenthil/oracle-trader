"""Job runner for Project Oracle's scheduled tasks.

There is no long-running daemon. Each task is a one-shot function invoked by
cron *inside the container* as ``python -m bees.bot <job>`` (see
/etc/cron.d/oracle), so memory is reclaimed after every run. Jobs:

  relogin   - refresh the Zerodha enctoken via headless login if it's expired
  signals   - end-of-day Donchian signal scan + switch alert emails
  summary   - daily portfolio summary email
  download  - daily market-data download (delegates to downloader.jobs)
  backup    - daily DB backup to Drive (delegates to downloader.jobs)
  momentum  - daily momentum advisory email (delegates to momentum.jobs)

Every job self-guards non-trading days (weekends + NSE holidays from
holiday.txt) via ``common.market_calendar`` — the market is closed, so there is
nothing to do.
"""
import datetime
import os
import sys

import pytz

from common.database import BrokerConfig, Strategy, PendingSwitch, Portfolio, CashFlow, init_db, get_db
from common.broker import is_zerodha_token_valid, clear_token_cache
from common.market_calendar import is_trading_day, skip_reason
from common.notifications import send_email
from bees.donchian import evaluate_donchian_intraday
from downloader.jobs import run_daily_download, run_db_backup
from momentum.jobs import run_daily_momentum_advisory

IST = pytz.timezone('Asia/Kolkata')


def ensure_enctoken():
    """Refresh the Zerodha enctoken via headless login when it isn't valid.

    Runs daily just before the market-close jobs. If a valid enctoken is already
    stored we do nothing; otherwise we drive a headless Kite login (see
    ``common.zerodha_login``), persist the fresh token to the same
    ``broker_config`` row the UI/API write to, and clear the validity cache so
    downstream jobs pick it up immediately.
    """
    now_ist = datetime.datetime.now(IST)
    reason = skip_reason(now_ist)
    if reason:
        print(f"Enctoken refresh skipped: {reason}.")
        return

    # The account we log into is whatever ZERODHA_USER_ID names (fetch_enctoken
    # uses the same env), so the fresh token must be stored against that user_id.
    db = next(get_db())
    cfg = db.query(BrokerConfig).filter(BrokerConfig.broker_name == "ZERODHA").first()
    user_id = (os.environ.get("ZERODHA_USER_ID") or "").strip() or (cfg.user_id if cfg else "") or "PC8006"
    if cfg and cfg.enctoken and is_zerodha_token_valid(cfg.enctoken, cfg.user_id or user_id):
        print("Enctoken refresh skipped: existing token is still valid.")
        db.close()
        return
    db.close()

    print(f"[{now_ist.strftime('%H:%M:%S')}] Enctoken missing/expired — logging in headlessly...")
    # Imported lazily so a missing Playwright install only breaks this one job.
    from common.zerodha_login import fetch_enctoken, ZerodhaLoginError
    try:
        enctoken = fetch_enctoken()
    except ZerodhaLoginError as exc:
        print(f"Headless login FAILED: {exc}")
        send_email(
            "<h2>🔑 Oracle enctoken refresh FAILED</h2>"
            f"<p>The automated Zerodha login could not obtain a fresh enctoken:</p>"
            f"<pre>{exc}</pre>"
            "<p>Update it manually in <b>Broker Setup</b> (or via the extension) so "
            "today's jobs can run.</p>",
            "🔑 Oracle enctoken refresh FAILED",
        )
        return

    db = next(get_db())
    try:
        cfg = db.query(BrokerConfig).filter(BrokerConfig.broker_name == "ZERODHA").first()
        if cfg:
            cfg.user_id = user_id
            cfg.enctoken = enctoken
        else:
            db.add(BrokerConfig(broker_name="ZERODHA", user_id=user_id, enctoken=enctoken))
        db.commit()
    finally:
        db.close()
    clear_token_cache()
    print(f"Enctoken refreshed for user_id={user_id} (len={len(enctoken)}).")


def check_intraday_signals():
    now_ist = datetime.datetime.now(IST)
    reason = skip_reason(now_ist)
    if reason:
        print(f"Signals skipped: {reason}.")
        return

    print(f"[{now_ist.strftime('%H:%M:%S')}] Running intraday check...")

    db = next(get_db())
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
    reason = skip_reason(datetime.datetime.now(IST))
    if reason:
        print(f"Daily summary skipped: {reason}.")
        return
    print("Generating daily 8:30 AM summary...")
    db = next(get_db())
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

        # Actual cash put in: deposits are negative amounts, withdrawals positive,
        # so net cash invested = -(sum of cash flows). Return on that cash is the
        # current holdings value vs. the cash committed.
        cash_flows = db.query(CashFlow).filter(CashFlow.strategy_id == strat.id).all()
        cash_invested = -sum(cf.amount for cf in cash_flows)
        cash_return = total_val - cash_invested
        cash_return_pct = ((total_val / cash_invested) - 1) * 100 if cash_invested > 0 else 0
        cash_color = 'green' if cash_return >= 0 else 'red'

        pending = db.query(PendingSwitch).filter(PendingSwitch.strategy_id == strat.id, PendingSwitch.status == 'PENDING').first()
        status_text = "<span style='color:red; font-weight:bold;'>PENDING BATCH SWITCH</span>" if pending else "<span style='color:green;'>All good, holding steady.</span>"

        html += f"""
        <div style="border: 1px solid #ddd; padding: 15px; margin-bottom: 20px;">
            <h3 style="margin-top: 0;">{strat.name}</h3>
            <p>Status: {status_text}</p>
            <table style="width: 100%; text-align: left;">
                <tr><th>{strat.asset1} Units</th><td>{asset1_port.units:.2f}</td></tr>
                <tr><th>{strat.asset2} Units</th><td>{asset2_port.units:.2f}</td></tr>
                <tr><th>Total Reinvested</th><td>₹{invested:,.2f}</td></tr>
                <tr><th>Cash Invested (net)</th><td>₹{cash_invested:,.2f}</td></tr>
                <tr><th>Current Value</th><td><b>₹{total_val:,.2f}</b></td></tr>
                <tr><th>Return on Cash Invested</th><td style="color: {cash_color};">₹{cash_return:,.2f} ({cash_return_pct:.2f}%)</td></tr>
                <tr><th>Overall ROI</th><td style="color: {'green' if roi >= 0 else 'red'};">{roi:.2f}%</td></tr>
            </table>
        </div>
        """

    db.close()
    send_email(html, "Project Oracle: Daily Portfolio State")


# Job name -> callable. Invoked one-shot by cron.
JOBS = {
    "relogin": ensure_enctoken,
    "signals": check_intraday_signals,
    "summary": send_daily_summary,
    "download": run_daily_download,
    "backup": run_db_backup,
    "momentum": run_daily_momentum_advisory,
}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in JOBS:
        print(f"usage: python -m bees.bot {{{'|'.join(JOBS)}}}")
        return 2
    init_db()  # bind SessionLocal (+ seed, idempotent) for this one-shot process
    JOBS[args[0]]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
