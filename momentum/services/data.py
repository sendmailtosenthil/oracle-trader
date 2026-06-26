"""Price/universe data layer for the momentum module.

Pure data access (no Streamlit, no DB). Ports quant-momentum's ``pricebook.js``,
``calendar.js`` and ``universe.js``:

- ``PriceBook``  — as-of (no look-ahead) close lookups, execution price proxy,
  daily-return volatility and bar-coverage counts over a window.
- ``Calendar``   — trading-day calendar built from the union of cached bar
  dates; ``months_back`` snaps N months back to the last trading day on/before.
- ``Universe``   — point-in-time index membership from dated constituent CSVs.

Price history is cached on disk as one JSON per symbol under
``DATA_ROOT/cache`` (``{symbol, token, fetchedAt, bars:[{date,open,high,low,
close,volume}, ...]}`` — bars oldest-first). ``refresh_prices`` re-fetches daily
candles from Zerodha via the shared ``common`` Kite client.
"""
import csv
import datetime
import glob
import json
import os

import pytz
import requests

IST = pytz.timezone("Asia/Kolkata")

DATA_ROOT = os.environ.get("MOMENTUM_DATA_ROOT", os.path.join("data", "momentum"))


def format_fetched(value):
    """Human-friendly 'fetchedAt' — handles full ISO datetimes and bare dates."""
    if not value:
        return "—"
    if "T" in value:
        try:
            return datetime.datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M IST")
        except ValueError:
            return value
    return value  # legacy date-only stamp


def cache_dir():
    return os.path.join(DATA_ROOT, "cache")


def constituents_dir():
    # Point-in-time Nifty500 membership ships WITH the module (static reference
    # data), not under the gitignored runtime data root. Override with env if needed.
    return os.environ.get(
        "MOMENTUM_CONSTITUENTS_DIR",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "constituents"),
    )


def to_yahoo(nse_symbol):
    """Normalise an NSE ticker to the cache key form (``MARUTI`` -> ``MARUTI.NS``)."""
    s = nse_symbol.strip().upper()
    return s if s.endswith(".NS") else f"{s}.NS"


def _add_months(d, months):
    """Subtract/add calendar months to a date (clamps day to month end)."""
    m = d.month - 1 + months
    year = d.year + m // 12
    month = m % 12 + 1
    # Clamp to the last valid day of the target month.
    if month == 12:
        nxt = datetime.date(year + 1, 1, 1)
    else:
        nxt = datetime.date(year, month + 1, 1)
    last_day = (nxt - datetime.timedelta(days=1)).day
    return datetime.date(year, month, min(d.day, last_day))


# ---------------------------------------------------------------------------
# Cache loading
# ---------------------------------------------------------------------------
def available_symbols():
    """All symbols with a cached price file (cache keys, e.g. ``MARUTI.NS``)."""
    files = glob.glob(os.path.join(cache_dir(), "*.json"))
    return sorted(os.path.basename(f)[:-5] for f in files)


def load_series(symbols=None):
    """Load ``{symbol -> bars}`` from the JSON cache. ``symbols`` filters the load.

    Bars are returned as lists of dicts (date/open/high/low/close/volume),
    sorted ascending by date.
    """
    series = {}
    if symbols is None:
        files = glob.glob(os.path.join(cache_dir(), "*.json"))
    else:
        files = [os.path.join(cache_dir(), f"{s}.json") for s in symbols]
    for path in files:
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                payload = json.load(f)
        except (ValueError, OSError):
            continue
        sym = payload.get("symbol") or os.path.basename(path)[:-5]
        bars = payload.get("bars") or []
        if bars:
            series[sym] = sorted(bars, key=lambda b: b["date"])
    return series


def prune_cache(keep_symbols):
    """Delete cached price files for symbols not in ``keep_symbols``.

    Keeps the cache to exactly the relevant set (current Nifty 500 ∪ holdings) so
    stale/delisted names don't pile up or skew coverage metrics. Returns the count
    removed.
    """
    keep = set(keep_symbols)
    removed = 0
    for path in glob.glob(os.path.join(cache_dir(), "*.json")):
        sym = os.path.basename(path)[:-5]
        if sym not in keep:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


def cache_meta():
    """Return (n_symbols, latest_fetched_at, latest_bar_date, earliest_bar_date)."""
    files = glob.glob(os.path.join(cache_dir(), "*.json"))
    latest_fetch = ""
    latest_bar = ""
    earliest_bar = ""
    for path in files:
        try:
            with open(path) as f:
                payload = json.load(f)
        except (ValueError, OSError):
            continue
        latest_fetch = max(latest_fetch, payload.get("fetchedAt") or "")
        bars = payload.get("bars") or []
        if bars:
            latest_bar = max(latest_bar, bars[-1]["date"])
            first = bars[0]["date"]
            earliest_bar = first if not earliest_bar else min(earliest_bar, first)
    return len(files), latest_fetch, latest_bar, earliest_bar


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------
class Calendar:
    """Trading-day calendar — the sorted set of dates seen in the price data."""

    def __init__(self, trading_dates):
        self.dates = sorted(set(trading_dates))

    @classmethod
    def from_series(cls, series):
        """Calendar from the union of all bar dates across every symbol."""
        all_dates = set()
        for bars in series.values():
            for b in bars:
                all_dates.add(b["date"])
        return cls(all_dates)

    def first(self):
        return self.dates[0] if self.dates else None

    def last(self):
        return self.dates[-1] if self.dates else None

    def on_or_before(self, iso):
        """Last trading day on or before ``iso`` (binary search), or None."""
        lo, hi = 0, len(self.dates)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.dates[mid] <= iso:
                lo = mid + 1
            else:
                hi = mid
        return self.dates[lo - 1] if lo - 1 >= 0 else None

    def months_back(self, iso, months):
        """N months before ``iso`` snapped to the last trading day on/before it."""
        d = datetime.date.fromisoformat(iso)
        back = _add_months(d, -months)
        return self.on_or_before(back.isoformat())

    def n_days_back(self, iso, n):
        """The trading day ``n`` positions before ``iso`` (clamped to the first)."""
        idx = self.on_or_before(iso)
        if idx is None:
            return None
        pos = self.dates.index(idx)
        return self.dates[max(0, pos - n)]


# ---------------------------------------------------------------------------
# PriceBook
# ---------------------------------------------------------------------------
class PriceBook:
    """In-memory price accessor over many symbols (no look-ahead).

    Stores **close only** (the single field the whole module uses — ranking,
    pricing and valuation are all close-based), so its RAM footprint is ~1/6 of
    the full OHLCV bars. Build it for the FULL universe with ``from_cache()`` or
    for a small subset by passing ``symbols`` — keep the subset small on a
    low-memory host (e.g. just holdings + the rebalance pool).
    """

    def __init__(self, series=None):
        self._dates = {}   # symbol -> [iso, ...] ascending
        self._close = {}   # symbol -> {iso: close}
        for sym, bars in (series or {}).items():
            self._add(sym, bars)

    def _add(self, sym, bars):
        ordered = sorted(bars, key=lambda b: b["date"])
        self._dates[sym] = [b["date"] for b in ordered]
        self._close[sym] = {b["date"]: b["close"] for b in ordered}

    @classmethod
    def from_cache(cls, symbols=None):
        """Build by streaming the JSON cache one file at a time (low peak RAM)."""
        pb = cls()
        if symbols is None:
            paths = glob.glob(os.path.join(cache_dir(), "*.json"))
        else:
            paths = [os.path.join(cache_dir(), f"{s}.json") for s in symbols]
        for path in paths:
            if not os.path.exists(path):
                continue
            try:
                with open(path) as f:
                    payload = json.load(f)
            except (ValueError, OSError):
                continue
            sym = payload.get("symbol") or os.path.basename(path)[:-5]
            bars = payload.get("bars") or []
            if bars:
                pb._add(sym, bars)
        return pb

    def has(self, symbol):
        return symbol in self._close

    def symbols(self):
        return list(self._close.keys())

    def all_dates(self):
        """Sorted union of all trading dates seen — the calendar."""
        s = set()
        for ds in self._dates.values():
            s.update(ds)
        return sorted(s)

    def _date_on_or_before(self, symbol, iso):
        dates = self._dates.get(symbol)
        if not dates:
            return None
        lo, hi = 0, len(dates)
        while lo < hi:
            mid = (lo + hi) // 2
            if dates[mid] <= iso:
                lo = mid + 1
            else:
                hi = mid
        return dates[lo - 1] if lo - 1 >= 0 else None

    def close_as_of(self, symbol, iso):
        d = self._date_on_or_before(symbol, iso)
        return self._close[symbol][d] if d else None

    def latest_close(self, symbol):
        dates = self._dates.get(symbol)
        return self._close[symbol][dates[-1]] if dates else None

    def exec_price(self, symbol, iso, side="buy"):
        """Execution price = close on ``iso`` (or the most recent prior close).

        Returns ``{price, date, exact}`` or None. Pricing is always the close
        (latest/running close intraday, settled close otherwise).
        """
        byd = self._close.get(symbol, {})
        if iso in byd:
            return {"price": byd[iso], "date": iso, "exact": True}
        d = self._date_on_or_before(symbol, iso)
        return {"price": byd[d], "date": d, "exact": False} if d else None

    def volatility(self, symbol, from_iso, to_iso):
        """Sample stdev of simple daily returns over [from_iso, to_iso]. None if sparse."""
        dates = self._dates.get(symbol)
        if not dates:
            return None
        byd = self._close[symbol]
        closes = [byd[d] for d in dates if from_iso <= d <= to_iso]
        if len(closes) < 6:
            return None
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1] > 0]
        if len(rets) < 5:
            return None
        mean = sum(rets) / len(rets)
        variance = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return variance ** 0.5

    def coverage(self, symbol, from_iso, to_iso):
        """Count of bars within [from_iso, to_iso] inclusive."""
        dates = self._dates.get(symbol)
        if not dates:
            return 0
        return sum(1 for d in dates if from_iso <= d <= to_iso)


def all_cached_dates():
    """Sorted union of all bar dates across the cache — built by streaming files
    (holds only the date set, never the bars). Used as the ranking calendar."""
    dates = set()
    for path in glob.glob(os.path.join(cache_dir(), "*.json")):
        try:
            with open(path) as f:
                payload = json.load(f)
        except (ValueError, OSError):
            continue
        for b in (payload.get("bars") or []):
            dates.add(b["date"])
    return sorted(dates)


class LazyPriceBook:
    """Close-only price accessor that loads **one symbol at a time** from disk.

    Exposes the same read methods ``score_universe`` uses (has / close_as_of /
    volatility / coverage). Because scoring touches a symbol's data in one
    consecutive burst, keeping just the last-loaded symbol gives O(1-symbol) RAM
    — so the whole 500-name universe can be scored in a few MB. ``from_cache``
    (the eager close-only book) remains for small symbol sets (holdings / pool).
    """

    def __init__(self):
        self._sym = None
        self._dates = []
        self._close = {}
        self._vol = {}

    def _load(self, symbol):
        if symbol == self._sym:
            return
        self._sym, self._dates, self._close, self._vol = symbol, [], {}, {}
        path = os.path.join(cache_dir(), f"{symbol}.json")
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                bars = (json.load(f).get("bars") or [])
        except (ValueError, OSError):
            return
        ordered = sorted(bars, key=lambda b: b["date"])
        self._dates = [b["date"] for b in ordered]
        self._close = {b["date"]: b["close"] for b in ordered}
        self._vol = {b["date"]: b.get("volume", 0) for b in ordered}

    def window(self, symbol, from_iso, to_iso):
        """(closes, volumes) for dates in [from_iso, to_iso] ascending — for the
        clenow/OBV models. Volumes are 0 where missing."""
        self._load(symbol)
        closes, vols = [], []
        for d in self._dates:
            if from_iso <= d <= to_iso:
                closes.append(self._close[d])
                vols.append(self._vol.get(d, 0))
        return closes, vols

    def symbols(self):
        return available_symbols()

    def has(self, symbol):
        self._load(symbol)
        return bool(self._dates)

    def _date_on_or_before(self, iso):
        dates = self._dates
        lo, hi = 0, len(dates)
        while lo < hi:
            mid = (lo + hi) // 2
            if dates[mid] <= iso:
                lo = mid + 1
            else:
                hi = mid
        return dates[lo - 1] if lo - 1 >= 0 else None

    def close_as_of(self, symbol, iso):
        self._load(symbol)
        d = self._date_on_or_before(iso)
        return self._close[d] if d else None

    def volatility(self, symbol, from_iso, to_iso):
        self._load(symbol)
        closes = [self._close[d] for d in self._dates if from_iso <= d <= to_iso]
        if len(closes) < 6:
            return None
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1] > 0]
        if len(rets) < 5:
            return None
        mean = sum(rets) / len(rets)
        variance = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return variance ** 0.5

    def coverage(self, symbol, from_iso, to_iso):
        self._load(symbol)
        return sum(1 for d in self._dates if from_iso <= d <= to_iso)


# ---------------------------------------------------------------------------
# Universe (point-in-time index membership)
# ---------------------------------------------------------------------------
class Universe:
    def __init__(self, snapshots):
        # snapshots: [{date, symbols:[...]}] ascending by date.
        self.snapshots = sorted(snapshots, key=lambda s: s["date"])

    @classmethod
    def load(cls):
        d = constituents_dir()
        snapshots = []
        if os.path.isdir(d):
            for path in sorted(glob.glob(os.path.join(d, "*.csv"))):
                name = os.path.basename(path)[:-4]
                # Only dated snapshot files (YYYY-MM-DD.csv).
                try:
                    datetime.date.fromisoformat(name)
                except ValueError:
                    continue
                snapshots.append({"date": name, "symbols": _parse_symbols(path)})
        return cls(snapshots)

    def all_symbols(self):
        """Every NSE symbol that appears in any snapshot (full historical universe)."""
        out = set()
        for snap in self.snapshots:
            out.update(snap["symbols"])
        return sorted(out)

    def latest(self):
        """Symbols in the most recent snapshot — the *current* index membership."""
        return list(self.snapshots[-1]["symbols"]) if self.snapshots else []

    def as_of(self, iso):
        """Membership effective on ``iso``: {symbols, snapshot_date, stale}."""
        chosen = None
        for snap in self.snapshots:
            if snap["date"] <= iso:
                chosen = snap
            else:
                break
        if not chosen:
            earliest = self.snapshots[0] if self.snapshots else {"date": "", "symbols": []}
            return {"symbols": earliest["symbols"], "snapshot_date": earliest["date"],
                    "stale": True, "before_first": True}
        return {"symbols": chosen["symbols"], "snapshot_date": chosen["date"],
                "stale": chosen["date"] != iso, "before_first": False}


def latest_reconstitution(today=None):
    """Most recent Nifty 500 reconstitution date (end of Mar / Sep) on/before today."""
    today = today or datetime.date.today()
    mar = datetime.date(today.year, 3, 31)
    sep = datetime.date(today.year, 9, 30)
    if today >= sep:
        return sep
    if today >= mar:
        return mar
    return datetime.date(today.year - 1, 9, 30)


def universe_status():
    """Health of the constituents file backing the momentum universe.

    Returns ``{ok, snapshot_date, count, stale, message}``. ``ok`` is False when
    the file is missing or implausibly small (can't be used); ``stale`` is True
    when a newer reconstitution has happened since the latest snapshot.
    """
    u = Universe.load()
    if not u.snapshots:
        return {"ok": False, "snapshot_date": None, "count": 0, "stale": False,
                "message": ("No Nifty 500 constituents found in momentum/constituents/. "
                            "Run `python scripts/fetch_nifty500_constituents.py` to download "
                            "the official list (or place ind_nifty500list.csv there).")}
    latest = u.snapshots[-1]
    count = len(latest["symbols"])
    if count < 400:
        return {"ok": False, "snapshot_date": latest["date"], "count": count, "stale": False,
                "message": (f"Constituents file looks incomplete ({count} symbols, expected ~500). "
                            "Re-fetch the official Nifty 500 list.")}
    expected = latest_reconstitution()
    stale = datetime.date.fromisoformat(latest["date"]) < expected
    msg = ""
    if stale:
        msg = (f"Constituents dated {latest['date']}, but a reconstitution occurred on "
               f"{expected.isoformat()} — the list may be stale. Re-run "
               "`python scripts/fetch_nifty500_constituents.py`.")
    return {"ok": True, "snapshot_date": latest["date"], "count": count, "stale": stale, "message": msg}


# Official NSE Nifty 500 constituents (Company Name,Industry,Symbol,Series,ISIN).
_NIFTY500_SOURCES = [
    "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
]
_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,application/octet-stream,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def download_official_nifty500(timeout=30):
    """Download the official Nifty 500 list from NSE. Returns (csv_text, symbols)
    or (None, None) if every source fails."""
    import requests
    for url in _NIFTY500_SOURCES:
        try:
            s = requests.Session()
            s.headers.update(_NSE_HEADERS)
            if "nse" in url:
                try:
                    s.get("https://www.nseindia.com", timeout=10)
                except Exception:
                    pass
            r = s.get(url, timeout=timeout)
            if r.status_code != 200:
                continue
            rows = list(csv.DictReader(io.StringIO(r.text)))
            syms = [(row.get("Symbol") or "").strip() for row in rows]
            syms = [x for x in syms if x]
            if 400 <= len(syms) <= 520:
                return r.text, syms
        except Exception:  # noqa: BLE001
            continue
    return None, None


def make_nse_session():
    """A requests session primed with NSE cookies (the archive CSVs need them).
    Reuse ONE session across many bhavcopy fetches to stay gentle on NSE."""
    import time as _time
    s = requests.Session()
    s.headers.update(_NSE_HEADERS)
    try:
        s.get("https://www.nseindia.com", timeout=10)   # 403 is fine — sets cookies
    except Exception:
        pass
    _time.sleep(1.0)   # let the cookie settle before hitting the archive
    return s


def fetch_delivery_bhavcopy(d, timeout=30, session=None):
    """Download NSE's one-per-day security bhavcopy for date ``d`` and return
    ``{nse_symbol: delivery_pct}`` for EQ series. Streamed line-by-line (low RAM).
    Returns None if the file isn't available (weekend/holiday/not-yet-published).

    Pass a shared ``session`` (from ``make_nse_session``) when fetching many days
    so cookies are primed once; otherwise a session is primed per call.
    """
    import codecs
    import time as _time
    s = session or make_nse_session()
    ddmmyyyy = d.strftime("%d%m%Y")
    urls = [
        f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv",
        f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv",
    ]
    for url in urls:
        for attempt in range(3):
            try:
                r = s.get(url, timeout=timeout, stream=True)
            except Exception:
                _time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 404:
                break   # no file for this date on this mirror — try the next mirror
            if r.status_code != 200:
                _time.sleep(2 * (attempt + 1))   # throttled — back off and retry
                continue
            reader = csv.reader(codecs.iterdecode(r.iter_lines(), "utf-8"))
            header = next(reader, None)
            if not header:
                break
            cols = {h.strip(): i for i, h in enumerate(header)}
            i_sym, i_ser, i_dlv = cols.get("SYMBOL"), cols.get("SERIES"), cols.get("DELIV_PER")
            if i_sym is None or i_ser is None or i_dlv is None:
                break
            out = {}
            for row in reader:
                if len(row) <= max(i_sym, i_ser, i_dlv):
                    continue
                if row[i_ser].strip() != "EQ":
                    continue
                try:
                    out[row[i_sym].strip().upper()] = float(row[i_dlv].strip())
                except ValueError:
                    continue  # '-' for series without delivery
            return out or None
    return None


def fetch_latest_delivery(max_back=6):
    """Fetch the most recent available delivery bhavcopy, trying today back
    ``max_back`` calendar days. Returns ``(iso_date, {symbol: pct})`` or (None, None)."""
    today = datetime.date.today()
    s = make_nse_session()
    for back in range(max_back + 1):
        d = today - datetime.timedelta(days=back)
        if d.weekday() >= 5:
            continue
        data = fetch_delivery_bhavcopy(d, session=s)
        if data:
            return d.isoformat(), data
    return None, None


def fetch_official_constituents(effective_date=None, timeout=30):
    """Download + cache the official list as ``<reconstitution-date>.csv``.

    Returns ``{ok, count, path, date, error}``.
    """
    eff = effective_date or latest_reconstitution()
    body, syms = download_official_nifty500(timeout=timeout)
    if not body:
        return {"ok": False, "count": 0, "path": None, "date": eff.isoformat(),
                "error": "Could not download the Nifty 500 list from NSE."}
    os.makedirs(constituents_dir(), exist_ok=True)
    out = os.path.join(constituents_dir(), f"{eff.isoformat()}.csv")
    try:
        with open(out, "w") as f:
            f.write(body)
    except OSError as exc:
        return {"ok": False, "count": len(syms), "path": out, "date": eff.isoformat(),
                "error": f"Downloaded but could not write {out}: {exc}"}
    return {"ok": True, "count": len(syms), "path": out, "date": eff.isoformat(), "error": ""}


def ensure_current_constituents(timeout=30):
    """Self-heal the universe: if the constituents file is missing or stale for the
    current reconstitution, download the official list. Network is touched ONLY when
    a refresh is actually needed. Returns ``{action, ...universe_status}``.
    """
    status = universe_status()
    if status["ok"] and not status["stale"]:
        return {"action": "none", **status}
    res = fetch_official_constituents(timeout=timeout)
    if res["ok"]:
        return {"action": "fetched", "fetched_date": res["date"], **universe_status()}
    return {"action": "failed", "error": res["error"], **status}


def _parse_symbols(path):
    out = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sym = row.get("Symbol") or row.get("symbol") or row.get("SYMBOL")
            if sym:
                out.append(sym.strip().upper())
    return out


# ---------------------------------------------------------------------------
# Price refresh (Zerodha)
# ---------------------------------------------------------------------------
def refresh_prices(enctoken, user_id="PC8006", symbols=None, history_from="2024-01-01",
                   progress_cb=None):
    """Refresh daily OHLC price caches from Zerodha for ``symbols``.

    Resolves each NSE symbol to its instrument token via the Kite instruments
    master, fetches daily candles from ``history_from`` to today, and writes the
    per-symbol JSON cache. Returns a summary dict. Symbols default to the
    *current* index membership (latest snapshot, ~500 names). Tolerates
    per-symbol failures (recorded in ``errors``).
    """
    from common.zerodha_client import ZerodhaClient, fetch_with_retry, FatalAuthError

    def emit(msg):
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    if symbols is None:
        symbols = [to_yahoo(s) for s in Universe.load().latest()]
    if isinstance(history_from, datetime.date):
        history_from = history_from.isoformat()

    result = {"updated": 0, "skipped": 0, "errors": [], "fatal": ""}
    os.makedirs(cache_dir(), exist_ok=True)

    try:
        emit("Loading NSE-equity token map (streamed)...")
        client = ZerodhaClient(enctoken, user_id=user_id, pace_seconds=0.2)
        if not client.validate():
            raise FatalAuthError("Invalid or expired enctoken.")
        # Slim, streamed token map (~2k entries) — not the full ~100k master.
        by_tsym = client.nse_eq_token_map()

        frm = datetime.datetime.combine(datetime.date.fromisoformat(history_from),
                                        datetime.time(9, 15))
        to = datetime.datetime.combine(datetime.date.today(), datetime.time(15, 30))
        # Single IST timestamp for this whole refresh run (when prices were pulled).
        fetched_ts = datetime.datetime.now(IST).isoformat(timespec="seconds")
        result["fetched_at"] = fetched_ts

        for i, sym in enumerate(symbols, 1):
            nse = sym[:-3] if sym.endswith(".NS") else sym
            token = by_tsym.get(nse)
            if not token:
                result["skipped"] += 1
                result["errors"].append(f"{sym}: no NSE token")
                continue
            try:
                emit(f"[{i}/{len(symbols)}] {sym}...")
                candles = fetch_with_retry(lambda: client.get_historical(token, "day", frm, to))
                new_bars = {str(c["timestamp"])[:10]:
                            {"date": str(c["timestamp"])[:10], "open": c["open"], "high": c["high"],
                             "low": c["low"], "close": c["close"], "volume": c["volume"]}
                            for c in candles}
                if not new_bars:
                    result["skipped"] += 1
                    continue
                # MERGE with any existing cached bars so a short-range fetch tops
                # up history instead of overwriting it (newer bars win on a date).
                path = os.path.join(cache_dir(), f"{sym}.json")
                merged = {}
                if os.path.exists(path):
                    try:
                        with open(path) as f:
                            for b in (json.load(f).get("bars") or []):
                                merged[b["date"]] = b
                    except (ValueError, OSError):
                        pass
                merged.update(new_bars)
                out_bars = [merged[d] for d in sorted(merged)]
                with open(path, "w") as f:
                    json.dump({"symbol": sym, "token": token,
                               "fetchedAt": fetched_ts,
                               "bars": out_bars}, f)
                result["updated"] += 1
            except FatalAuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"{sym}: {exc}")
    except FatalAuthError as exc:
        result["fatal"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"Refresh failed: {exc}")

    return result
