"""Import the live momentum portfolio from the quant-downloader stats DB.

The 15-stock book bought on 2026-06-22 lives in quant-downloader's
``data/stats.db`` (``momentum_portfolio`` snapshot; the ``momentum_trades`` log
was never written). We reconstruct one BUY trade per holding at its ``avg_price``
on ``entry_date``, attach the Zerodha delivery charges that were actually paid,
and let the trade ledger drive holdings + cash.

Idempotent: clears existing momentum trades/holdings before importing. Run with:
    python scripts/import_momentum_portfolio.py [path/to/stats.db]
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import database
from common.database import MomentumTrade, MomentumHolding
from momentum.services import strategy

DEFAULT_SRC = "/Users/senthil/Documents/Personal/projects/quant-downloader/data/stats.db"
CAPITAL = 225000.0  # quant-downloader momentum config.investment


def _ensure_charges_column():
    """Add momentum_trades.charges to a pre-existing oracle.db (create_all won't)."""
    con = sqlite3.connect("oracle.db")
    cols = [r[1] for r in con.execute("PRAGMA table_info(momentum_trades)").fetchall()]
    if "charges" not in cols:
        con.execute("ALTER TABLE momentum_trades ADD COLUMN charges FLOAT DEFAULT 0.0")
        con.commit()
        print("Added momentum_trades.charges column.")
    con.close()


def main(src=DEFAULT_SRC):
    if not os.path.exists(src):
        raise SystemExit(f"Source stats DB not found: {src}")

    database.init_db("sqlite:///oracle.db")
    _ensure_charges_column()
    db = next(database.get_db())

    src_con = sqlite3.connect(src)
    src_con.row_factory = sqlite3.Row
    holdings = src_con.execute(
        "SELECT symbol, shares, avg_price, entry_date, entry_rank "
        "FROM momentum_portfolio ORDER BY entry_rank"
    ).fetchall()
    src_con.close()
    if not holdings:
        raise SystemExit("No holdings found in source momentum_portfolio.")

    # Reset the oracle momentum books, then import.
    db.query(MomentumTrade).delete()
    db.query(MomentumHolding).delete()
    db.commit()

    total_value = total_charges = 0.0
    for h in holdings:
        value = h["shares"] * h["avg_price"]
        charges = strategy.zerodha_charges(h["avg_price"], h["shares"], "buy")
        total_value += value
        total_charges += charges
        db.add(MomentumTrade(
            date=h["entry_date"], symbol=h["symbol"], side="BUY",
            shares=h["shares"], price=h["avg_price"], value=value,
            charges=charges, rank=h["entry_rank"], reason="deploy",
        ))
    db.commit()

    cfg = strategy.get_config(db)
    cfg.investment = CAPITAL
    cfg.capital_injected = 0.0
    cfg.num_stocks = 15
    db.commit()

    strategy.recalc_holdings(db)
    cash = strategy.recalc_cash(db, cfg)

    print(f"Imported {len(holdings)} holdings.")
    print(f"  Capital        : ₹{CAPITAL:,.2f}")
    print(f"  Holdings cost  : ₹{total_value:,.2f}")
    print(f"  Total charges  : ₹{total_charges:,.2f}")
    print(f"  Reconciled cash: ₹{cash:,.2f}  (quant-downloader stored 230.27)")
    print(f"  Equity (cost)  : ₹{total_value + cash:,.2f}")
    db.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SRC)
