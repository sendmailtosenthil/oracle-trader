"""One-time migration: add `charges`/`charges_breakdown` to trades and backfill.

Usage (from the project root, with the `bees` package importable):

    python -m migrations.add_trade_charges [path-to-db]

Defaults to ``oracle.db`` in the current directory. Safe to re-run: it resets
each trade to its freshly-computed base charges, then re-applies pledge charges
for completed switches, so the result is identical every time (idempotent).

What it does:
  1. ALTER TABLE trades ADD COLUMN charges / charges_breakdown (if missing).
  2. Compute Zerodha charges (ETF-specific STT, txn, SEBI, stamp, GST, DP) for
     every existing trade.
  3. Add the pledge request charge to the last BUY of each COMPLETED switch.
"""
import json
import sqlite3
import sys

from bees import database
from bees.services.charges import compute_trade_charges, ticker_for, pledge_charge


def _column_exists(conn, table, col):
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(r[1] == col for r in cur.fetchall())


def ensure_columns(db_path):
    """Add the new columns directly via SQLite (SQLAlchemy won't ALTER existing tables)."""
    conn = sqlite3.connect(db_path)
    try:
        if not _column_exists(conn, 'trades', 'charges'):
            conn.execute("ALTER TABLE trades ADD COLUMN charges FLOAT DEFAULT 0")
            print("Added column trades.charges")
        if not _column_exists(conn, 'trades', 'charges_breakdown'):
            conn.execute("ALTER TABLE trades ADD COLUMN charges_breakdown VARCHAR")
            print("Added column trades.charges_breakdown")
        conn.commit()
    finally:
        conn.close()


def backfill(db_path):
    database.init_db(f'sqlite:///{db_path}')
    db = next(database.get_db())
    from bees.database import Strategy, Trade, PendingSwitch

    strategies = {s.id: s for s in db.query(Strategy).all()}
    trades = db.query(Trade).order_by(Trade.date.asc()).all()

    # 1. Base charges for every trade (resets any prior value -> idempotent)
    for t in trades:
        strat = strategies.get(t.strategy_id)
        if not strat:
            continue
        ticker = ticker_for(strat, t.asset)
        total, breakdown = compute_trade_charges(ticker, t.trade_type, t.units, t.price)
        t.charges = total
        t.charges_breakdown = breakdown
    db.commit()
    print(f"Backfilled base charges for {len(trades)} trade(s).")

    # 2. Pledge charge on the last BUY of each COMPLETED full switch
    completed = db.query(PendingSwitch).filter(PendingSwitch.status == 'COMPLETED').all()
    pledge_amt = pledge_charge()
    pledged_ids = set()
    applied = 0
    for sw in completed:
        buys = [t for t in trades
                if t.strategy_id == sw.strategy_id and t.asset == sw.to_asset and t.trade_type == 'BUY']
        # Prefer the buys that executed on/after the switch was raised.
        after = [t for t in buys if sw.created_at is None or t.date >= sw.created_at]
        pool = sorted(after or buys, key=lambda t: t.date, reverse=True)
        target = next((t for t in pool if t.id not in pledged_ids), None)
        if not target:
            print(f"  Switch {sw.id}: no eligible BUY of {sw.to_asset} found; skipping pledge.")
            continue

        breakdown = json.loads(target.charges_breakdown) if target.charges_breakdown else {}
        breakdown['pledge'] = round(breakdown.get('pledge', 0.0) + pledge_amt, 2)
        target.charges = round((target.charges or 0.0) + pledge_amt, 2)
        breakdown['total'] = target.charges
        target.charges_breakdown = json.dumps(breakdown)
        pledged_ids.add(target.id)
        applied += 1
        print(f"  Switch {sw.id}: pledge ₹{pledge_amt:.2f} added to trade {target.id} (BUY {sw.to_asset}).")
    db.commit()
    print(f"Applied pledge charges to {applied} trade(s) from {len(completed)} completed switch(es).")

    grand_total = sum((t.charges or 0.0) for t in trades)
    print(f"Total charges across all trades: ₹{grand_total:,.2f}")
    db.close()


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'oracle.db'
    print(f"== Charges migration on {path} ==")
    ensure_columns(path)
    backfill(path)
    print("Done.")


if __name__ == '__main__':
    main()
