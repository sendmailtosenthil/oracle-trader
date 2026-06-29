"""Backfill historical NSE delivery % into the DB (one bhavcopy per trading day).

Delivery % powers the 'delivery' scoring model (and a future delivery-OBV). NSE
publishes it as a single daily security bhavcopy (all stocks in one file), so we
fetch ONE file per trading day — slowly, to be polite to NSE — and store the
parsed delivery % in the ``momentum_delivery`` table (on the boot disk, inside
oracle.db).

Rate-limited and RESUMABLE: dates already stored are skipped (no network), and a
configurable pause separates downloads. Safe to re-run / resume after an abort.

Run on the host where oracle.db lives (e.g. the VPS):
    python scripts/backfill_delivery.py [days_back=90] [pace_seconds=3.0]
"""
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import database
from common.database import MomentumDelivery
from momentum.services import data as mdata


def main(days_back=90, pace=3.0):
    database.init_db("sqlite:///oracle.db")
    db = next(database.get_db())
    today = datetime.datetime.now(mdata.IST).date()
    session = mdata.make_nse_session()   # prime cookies ONCE, reuse for all days

    fetched = skipped = nofile = errors = 0
    for back in range(days_back + 1):
        d = today - datetime.timedelta(days=back)
        if d.weekday() >= 5:          # weekend — no session
            continue
        iso = d.isoformat()
        if db.query(MomentumDelivery).filter(MomentumDelivery.date == iso).count():
            skipped += 1
            continue
        try:
            data = mdata.fetch_delivery_bhavcopy(d, session=session)
        except Exception as exc:       # noqa: BLE001
            errors += 1
            print(f"{iso}: error — {exc}")
            time.sleep(pace)
            continue
        if not data:
            nofile += 1
            print(f"{iso}: no file (holiday / not published)")
            time.sleep(pace)
            continue
        for sym, pct in data.items():
            db.add(MomentumDelivery(date=iso, symbol=sym, deliv_pct=pct))
        db.commit()
        fetched += 1
        print(f"{iso}: stored {len(data)} stocks")
        time.sleep(pace)               # be gentle on NSE

    total = db.query(MomentumDelivery).count()
    distinct_days = db.query(MomentumDelivery.date).distinct().count()
    print(f"\nDone. fetched={fetched}, already-stored={skipped}, "
          f"no-file={nofile}, errors={errors}")
    print(f"DB now holds {total} delivery rows across {distinct_days} trading day(s).")
    db.close()


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    pace = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    main(days, pace)
