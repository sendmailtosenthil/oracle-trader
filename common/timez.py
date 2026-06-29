"""Single source of truth for "now" in the only timezone this app cares about.

Every market date, scheduling decision and "today" comparison in Project Oracle
is about the *Indian* trading day. The host clock is not guaranteed to be IST
(VPS images default to UTC), and `datetime.date.today()` / `datetime.now()`
follow that host clock — so they silently produce the *wrong* day for ~5.5h
each night. Always go through these helpers instead of the stdlib calls.
"""
import datetime

import pytz

IST = pytz.timezone("Asia/Kolkata")


def now_ist():
    """Current timezone-aware datetime in IST."""
    return datetime.datetime.now(IST)


def today_ist():
    """Current calendar date in IST (the Indian trading day)."""
    return datetime.datetime.now(IST).date()


def to_ist(dt):
    """Render a stored timestamp in IST for display.

    DB timestamps are stored naive-UTC (``datetime.utcnow``); attach UTC then
    convert. A naive datetime is assumed to be UTC; an aware one is converted.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=pytz.utc)
    return dt.astimezone(IST)
