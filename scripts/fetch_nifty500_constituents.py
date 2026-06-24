"""Download the official NSE Nifty 500 constituents and cache them.

The momentum universe must be the *current* Nifty 500 membership. The app
self-heals this on load (``data.ensure_current_constituents``); this script is
the manual equivalent — useful for a one-off fetch or to force a specific
reconstitution date. Nifty 500 reconstitutes semi-annually (end of Mar / Sep).

    python scripts/fetch_nifty500_constituents.py            # latest reconstitution
    python scripts/fetch_nifty500_constituents.py 2026-09-30 # explicit date
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from momentum.services import data as mdata


def main(date_str=None):
    eff = datetime.date.fromisoformat(date_str) if date_str else mdata.latest_reconstitution()
    print(f"Fetching official Nifty 500 constituents (effective {eff})...")
    res = mdata.fetch_official_constituents(effective_date=eff)
    if not res["ok"]:
        raise SystemExit(
            res["error"] + "\n\nManual steps:\n"
            "  1. Open https://www.niftyindices.com/indices/equity/broad-based-indices/nifty-500\n"
            "  2. Download the constituents CSV (ind_nifty500list.csv).\n"
            f"  3. Save it as: {os.path.join(mdata.constituents_dir(), eff.isoformat() + '.csv')}\n"
        )
    print(f"Saved {res['count']} symbols -> {res['path']}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
