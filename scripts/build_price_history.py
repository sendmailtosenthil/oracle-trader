"""Headless build of the momentum PRICE history (Clenow + all price-based models).

Fetches ~15 months of daily OHLC for the current Nifty 500 ∪ holdings from
Zerodha (using the enctoken stored in oracle.db), prunes the cache to that set,
and recomputes + persists the ranking. This is the CLI equivalent of the
"Build / repair full price history" button — handy for first setup on the VPS.

Clenow needs NO separate dataset: it reads the same daily closes as the other
models, just over its own (calendar-day) window. So this one command covers
Clenow, risk_adjusted, OBV, and the price side of delivery/blended.

Run on the host where oracle.db lives (needs a valid Zerodha enctoken set via
Broker Setup):
    python scripts/build_price_history.py [days_back=455]
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import database
from common.database import BrokerConfig, MomentumHolding
from momentum.services import data as mdata
from momentum.services import strategy


def main(days_back=455):
    database.init_db("sqlite:///oracle.db")
    db = next(database.get_db())

    bc = db.query(BrokerConfig).filter(BrokerConfig.broker_name == "ZERODHA").first()
    if not bc or not bc.enctoken:
        raise SystemExit("No Zerodha enctoken in oracle.db — set it in Broker Setup first.")

    # Make sure the universe file is current (downloads only if missing/stale).
    mdata.ensure_current_constituents()
    current = {mdata.to_yahoo(s) for s in mdata.Universe.load().latest()}
    held = {h.symbol for h in db.query(MomentumHolding).filter(MomentumHolding.shares > 0).all()}
    syms = sorted(current | held)
    start = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()

    print(f"Fetching {len(syms)} symbols (current Nifty 500 + holdings) from {start} → today...")
    result = mdata.refresh_prices(
        enctoken=bc.enctoken, user_id=bc.user_id, symbols=syms,
        history_from=start, progress_cb=lambda m: print("  " + m),
    )
    if result["fatal"]:
        raise SystemExit("Fatal: " + result["fatal"])

    pruned = mdata.prune_cache(syms)
    print(f"Updated {result['updated']}, skipped {result['skipped']}, pruned {pruned} stale.")
    if result["errors"]:
        print(f"{len(result['errors'])} warning(s); first few: {result['errors'][:5]}")

    print("Re-ranking (streamed) and persisting to DB...")
    r = strategy.compute_ranking(db)
    print(f"Done. Ranked {len(r['ranked'])} of {r['n_universe']} as of {r['as_of']}.")
    db.close()


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 455
    main(days)
