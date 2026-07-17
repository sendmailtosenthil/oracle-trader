"""NSE trading-day calendar for Project Oracle.

The market is closed on weekends and on a fixed list of annual holidays, so the
scheduled jobs that only make sense on a trading day (data download, EOD
signals, DB backup, momentum advisory, the daily summary, and the enctoken
refresh) consult :func:`is_trading_day` and no-op otherwise.

Holidays live in ``holiday.txt`` at the repo root — one date per line in NSE's
``DD-Mon-YY`` format (e.g. ``15-Jan-26``). A missing/empty file degrades
gracefully to "weekends only". The file is small and read on demand; results
are cached per-process so repeated calls in one job don't re-read it.
"""
import datetime
import os

import pytz

IST = pytz.timezone("Asia/Kolkata")

# holiday.txt sits next to the repo root (this file is common/market_calendar.py).
_HOLIDAY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "holiday.txt"
)

_cache = {"mtime": None, "holidays": frozenset()}


def _load_holidays(path=_HOLIDAY_FILE):
    """Parsed set of holiday ``date`` objects, cached until the file changes."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return frozenset()  # no file → weekends only
    if _cache["mtime"] == mtime:
        return _cache["holidays"]

    holidays = set()
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    holidays.add(datetime.datetime.strptime(line, "%d-%b-%y").date())
                except ValueError:
                    continue  # ignore malformed lines rather than crash a job
    except OSError:
        return frozenset()

    _cache["mtime"] = mtime
    _cache["holidays"] = frozenset(holidays)
    return _cache["holidays"]


def _as_date(value):
    if value is None:
        return datetime.datetime.now(IST).date()
    if isinstance(value, datetime.datetime):
        return value.date()
    return value


def is_holiday(value=None):
    """True if the given date (default: today IST) is a listed NSE holiday."""
    return _as_date(value) in _load_holidays()


def is_trading_day(value=None):
    """True on NSE trading days — Mon–Fri and not a listed holiday.

    ``value`` may be a ``date`` or ``datetime``; defaults to now in IST.
    """
    d = _as_date(value)
    return d.weekday() < 5 and d not in _load_holidays()


def skip_reason(value=None):
    """Human-readable reason the market is closed, or ``None`` if it's open."""
    d = _as_date(value)
    if d.weekday() >= 5:
        return "weekend"
    if d in _load_holidays():
        return "market holiday"
    return None
