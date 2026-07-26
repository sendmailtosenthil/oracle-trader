"""Shared view helpers for the Zerodha Trades pages."""
import datetime

import pytz
import streamlit as st

from zerodha_trades.services import positions

IST = pytz.timezone("Asia/Kolkata")

STATUS_BADGE = {
    'draft': ":gray-badge[Draft]",
    'deployed': ":blue-badge[Deployed]",
    'triggered': ":red-badge[Triggered]",
}


# Inside a form, Streamlit paints a "Press Enter to submit form" hint as an
# overlay *within* the input box. In the narrow stoploss/target columns it sits
# straight on top of the number being typed. Hiding it keeps the value legible;
# Enter still submits, and the forms have explicit buttons anyway.
_CSS = """
<style>
div[data-testid="InputInstructions"] { display: none !important; }
</style>
"""


def inject_css():
    st.markdown(_CSS, unsafe_allow_html=True)


_FLASH = "_ztrade_flash"


def flash(kind, message):
    """Queue a message to show after the next ``st.rerun()``.

    A rerun restarts the script immediately, so anything written with
    ``st.success`` / ``st.error`` right before one is never painted. Mutation
    handlers queue their feedback here instead and :func:`render_flash` emits it
    on the following run.
    """
    st.session_state.setdefault(_FLASH, []).append((kind, message))


def render_flash():
    """Emit and clear any queued messages. Call once at the top of a page."""
    for kind, message in st.session_state.pop(_FLASH, []):
        getattr(st, kind)(message)


def money(value):
    """Signed rupee amount, e.g. ``+₹1,234.50`` / ``-₹980.00``."""
    if value is None:
        return "—"
    return f"{'+' if value >= 0 else '-'}₹{abs(value):,.2f}"


def colored_money(value):
    """:func:`money` wrapped in Streamlit's green/red markdown colouring."""
    if value is None:
        return "—"
    return f":{'green' if value >= 0 else 'red'}[{money(value)}]"


def ist(dt):
    """Render a naive UTC timestamp as IST wall-clock."""
    if dt is None:
        return "never"
    return pytz.utc.localize(dt).astimezone(IST).strftime("%d %b %H:%M:%S")


# max_entries caps the cache at the current + previous token, so daily enctoken
# rotation can't accumulate dead entries on a memory-tight host.
@st.cache_data(ttl=20, max_entries=2, show_spinner=False)
def _fetch_cached(enctoken, user_id):
    """Positions + fetch time, memoised for 20s on the credentials."""
    rows = positions.fetch(enctoken, user_id)
    return rows, datetime.datetime.utcnow()


def clear_positions_cache():
    _fetch_cached.clear()


# Lot sizes only change when the exchange revises a contract, so an hours-long
# TTL is plenty and keeps the instruments dump off the wire on every rerun.
@st.cache_data(ttl=21600, max_entries=4, show_spinner=False)
def _lot_sizes_cached(enctoken, user_id, symbols):
    return positions.lot_sizes(enctoken, user_id, symbols)


def lot_sizes(db, symbols):
    """``{tradingsymbol: lot_size}``, or ``{}`` if they can't be resolved.

    Degrading to an empty map is deliberate: quantity validation then skips the
    whole-lots rule rather than blocking the user on a broker hiccup.
    """
    symbols = tuple(sorted({s for s in symbols if s}))
    enctoken, user_id = positions.broker_credentials(db)
    if not enctoken or not symbols:
        return {}
    try:
        return _lot_sizes_cached(enctoken, user_id, symbols)
    except Exception:  # noqa: BLE001 - validation relaxes, page still works
        return {}


def live_positions(db):
    """``(positions, fetched_at, error)`` — live book, snapshotted to the DB.

    Never raises: a broker/auth failure comes back as the third element so the
    page can still render its groups off stored state.
    """
    enctoken, user_id = positions.broker_credentials(db)
    if not enctoken:
        return [], None, "No Zerodha enctoken configured — set one in **Broker Setup**."
    try:
        rows, fetched_at = _fetch_cached(enctoken, user_id)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
        return [], None, f"Could not fetch positions: {exc}"

    # Persist only genuinely new fetches, so a cached read doesn't refresh the
    # snapshot's updated_at and make stale data look current.
    if st.session_state.get("_ztrade_last_fetch") != fetched_at:
        positions.save_snapshot(db, rows)
        st.session_state["_ztrade_last_fetch"] = fetched_at
    return rows, fetched_at, None
