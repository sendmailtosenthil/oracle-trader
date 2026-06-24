"""Download the official NSE Nifty 500 constituents and cache them.

The momentum universe must be the *current* Nifty 500 membership. NSE
(niftyindices.com) publishes the authoritative list as ``ind_nifty500list.csv``
(columns: Company Name, Industry, Symbol, Series, ISIN Code). We download it,
sanity-check it (~500 symbols), and write it into ``momentum/constituents/`` as
``<reconstitution-date>.csv`` so ``Universe.as_of`` picks it for current dates.

Nifty 500 reconstitutes semi-annually (end of March / end of September), so the
file is dated to the most recent reconstitution on/before today. Re-run this
once per reconstitution (or whenever NSE changes the list). Usage:

    python scripts/fetch_nifty500_constituents.py          # auto date
    python scripts/fetch_nifty500_constituents.py 2026-09-30
"""
import csv
import datetime
import io
import os
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CONSTITUENTS_DIR = os.path.join(os.path.dirname(HERE), "momentum", "constituents")
SOURCES = [
    "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,application/octet-stream,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def latest_reconstitution(today=None):
    today = today or datetime.date.today()
    mar = datetime.date(today.year, 3, 31)
    sep = datetime.date(today.year, 9, 30)
    if today >= sep:
        return sep
    if today >= mar:
        return mar
    return datetime.date(today.year - 1, 9, 30)


def download():
    for url in SOURCES:
        try:
            s = requests.Session()
            s.headers.update(HEADERS)
            if "nse" in url:
                try:
                    s.get("https://www.nseindia.com", timeout=10)
                except Exception:
                    pass
            r = s.get(url, timeout=30)
            if r.status_code != 200:
                print(f"  {url} -> HTTP {r.status_code}")
                continue
            rows = list(csv.DictReader(io.StringIO(r.text)))
            syms = [(row.get("Symbol") or "").strip() for row in rows]
            syms = [x for x in syms if x]
            if 400 <= len(syms) <= 520:
                print(f"  {url} -> OK ({len(syms)} symbols)")
                return r.text, syms
            print(f"  {url} -> unexpected symbol count {len(syms)}")
        except Exception as e:  # noqa: BLE001
            print(f"  {url} -> {type(e).__name__}: {e}")
    return None, None


def main(date_str=None):
    eff = datetime.date.fromisoformat(date_str) if date_str else latest_reconstitution()
    print(f"Fetching official Nifty 500 constituents (effective {eff})...")
    body, syms = download()
    if body is None:
        raise SystemExit(
            "Could not download the Nifty 500 list from NSE.\n"
            "Manual steps:\n"
            "  1. Open https://www.niftyindices.com/indices/equity/broad-based-indices/nifty-500\n"
            "  2. Download 'ind_nifty500list.csv' (or the constituents CSV).\n"
            f"  3. Save it as: {os.path.join(CONSTITUENTS_DIR, eff.isoformat() + '.csv')}\n"
            "  4. Re-run nothing — it will be picked up on next ranking."
        )
    os.makedirs(CONSTITUENTS_DIR, exist_ok=True)
    out = os.path.join(CONSTITUENTS_DIR, f"{eff.isoformat()}.csv")
    with open(out, "w") as f:
        f.write(body)
    print(f"Saved {len(syms)} symbols -> {out}")
    print(f"Sample: {syms[:5]}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
