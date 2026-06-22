"""One-time migration: add `charges`/`charges_breakdown` to trades and backfill.

Usage (from the project root, with the `bees` package importable):

    python -m migrations.add_trade_charges [path-to-db]

Defaults to ``oracle.db`` in the current directory. Safe to re-run: it resets
each trade to its freshly-computed base charges, then re-applies pledge charges
for completed switches, so the result is identical every time (idempotent).

What it does:
  1. ALTER TABLE trades ADD COLUMN charges / charges_breakdown / pledge (if missing).
  2. Compute Zerodha charges for every trade: ETF-specific STT, exchange txn,
     SEBI, stamp duty (buy), GST, and DP (once per instrument per day, on that
     day's last sell).
  Pledge charges are NOT auto-applied here — tick a trade's "Pledge" checkbox in
  the Ledger after backfill and it will be added on the next save.
"""
import sqlite3
import sys

from bees import database
from bees.services.charges import reconcile_strategy_charges


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
        if not _column_exists(conn, 'trades', 'pledge'):
            conn.execute("ALTER TABLE trades ADD COLUMN pledge BOOLEAN DEFAULT 0")
            print("Added column trades.pledge")
        conn.commit()
    finally:
        conn.close()


def backfill(db_path):
    database.init_db(f'sqlite:///{db_path}')
    db = next(database.get_db())
    from bees.database import Strategy, Trade

    strategies = db.query(Strategy).all()
    for strat in strategies:
        reconcile_strategy_charges(db, strat.id)
        print(f"Reconciled charges for strategy {strat.id} ({strat.name}).")

    trades = db.query(Trade).all()
    grand_total = sum((t.charges or 0.0) for t in trades)
    print(f"Reconciled {len(trades)} trade(s) across {len(strategies)} strateg(ies).")
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
