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

DATA_ROOT = os.environ.get("MOMENTUM_DATA_ROOT", os.path.join("data", "momentum"))


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


def cache_meta():
    """Return (n_symbols, latest_fetched_at, latest_bar_date) across the cache."""
    files = glob.glob(os.path.join(cache_dir(), "*.json"))
    latest_fetch = ""
    latest_bar = ""
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
    return len(files), latest_fetch, latest_bar


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


# ---------------------------------------------------------------------------
# PriceBook
# ---------------------------------------------------------------------------
class PriceBook:
    """In-memory price accessor over many symbols (no look-ahead)."""

    def __init__(self, series, pricing=None):
        # pricing: {'buy': 'OPEN'|'CLOSE'|'HL2'|'OH2'|'OL2', 'sell': ...}
        self.pricing = pricing or {"buy": "OPEN", "sell": "OPEN"}
        self._dates = {}    # symbol -> [iso, ...] ascending
        self._by_date = {}  # symbol -> {iso: bar}
        for sym, bars in series.items():
            ordered = sorted(bars, key=lambda b: b["date"])
            self._dates[sym] = [b["date"] for b in ordered]
            self._by_date[sym] = {b["date"]: b for b in ordered}

    def has(self, symbol):
        return symbol in self._by_date

    def bar_on(self, symbol, iso):
        return self._by_date.get(symbol, {}).get(iso)

    def as_of(self, symbol, iso):
        """Last bar on or before ``iso``, or None."""
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
        idx = lo - 1
        if idx < 0:
            return None
        return self._by_date[symbol][dates[idx]]

    def close_as_of(self, symbol, iso):
        bar = self.as_of(symbol, iso)
        return bar["close"] if bar else None

    def latest_close(self, symbol):
        dates = self._dates.get(symbol)
        if not dates:
            return None
        return self._by_date[symbol][dates[-1]]["close"]

    def _price_from_bar(self, bar, mode):
        if mode == "CLOSE":
            return bar["close"]
        if mode == "HL2":
            return (bar["high"] + bar["low"]) / 2
        if mode == "OH2":
            return (bar["open"] + bar["high"]) / 2
        if mode == "OL2":
            return (bar["open"] + bar["low"]) / 2
        return bar["open"]  # OPEN (default)

    def exec_price(self, symbol, iso, side):
        """Execution price for ``side`` ('buy'/'sell') using the configured proxy.

        Uses the exact bar on ``iso``; falls back to the most recent prior bar.
        Returns ``{price, date, exact}`` or None.
        """
        bar = self.bar_on(symbol, iso)
        exact = True
        if not bar:
            bar = self.as_of(symbol, iso)
            exact = False
        if not bar:
            return None
        mode = self.pricing["buy"] if side == "buy" else self.pricing["sell"]
        return {"price": self._price_from_bar(bar, mode), "date": bar["date"], "exact": exact}

    def volatility(self, symbol, from_iso, to_iso):
        """Sample stdev of simple daily returns over [from_iso, to_iso]. None if sparse."""
        dates = self._dates.get(symbol)
        if not dates:
            return None
        byd = self._by_date[symbol]
        closes = [byd[d]["close"] for d in dates if from_iso <= d <= to_iso]
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

    result = {"updated": 0, "skipped": 0, "errors": [], "fatal": ""}
    os.makedirs(cache_dir(), exist_ok=True)

    try:
        emit("Loading Kite instruments master...")
        client = ZerodhaClient(enctoken, user_id=user_id, pace_seconds=0.2)
        if not client.validate():
            raise FatalAuthError("Invalid or expired enctoken.")
        client.load_instruments()

        # Index NSE equities by tradingsymbol for token resolution.
        by_tsym = {}
        for item in client.instruments:
            if item.get("segment") == "NSE" and item.get("instrument_type") == "EQ":
                by_tsym[item["tradingsymbol"]] = item["instrument_token"]

        frm = datetime.datetime.combine(datetime.date.fromisoformat(history_from),
                                        datetime.time(9, 15))
        to = datetime.datetime.combine(datetime.date.today(), datetime.time(15, 30))

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
                bars = [{"date": str(c["timestamp"])[:10], "open": c["open"], "high": c["high"],
                         "low": c["low"], "close": c["close"], "volume": c["volume"]}
                        for c in candles]
                if not bars:
                    result["skipped"] += 1
                    continue
                with open(os.path.join(cache_dir(), f"{sym}.json"), "w") as f:
                    json.dump({"symbol": sym, "token": token,
                               "fetchedAt": datetime.date.today().isoformat(),
                               "bars": bars}, f)
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
