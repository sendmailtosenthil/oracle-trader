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
/* The -/+ steppers are focusable, so Tab lands on them instead of moving
   to the next field. Hiding them takes them out of the focus order; the
   value is still typed directly. */
button[data-testid="stNumberInputStepUp"],
button[data-testid="stNumberInputStepDown"] { display: none !important; }
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


# Keyed on the full credential set so rotating any one token refetches all.
# max_entries stays tiny: this holds a few dozen small dicts per account.
@st.cache_data(ttl=20, max_entries=2, show_spinner=False)
def _fetch_all_cached(creds):
    """Every account's book, fetched in parallel (max 5 at a time)."""
    return positions.fetch_many(list(creds)), datetime.datetime.utcnow()


def clear_positions_cache():
    _fetch_all_cached.clear()


def live_positions_by_account(db, only=None):
    """``({user_id: {...}}, fetched_at)`` for the configured accounts.

    Each entry is ``{'positions': [...], 'error': str|None}``. Accounts are
    fetched concurrently and one bad token only marks its own entry, so a stale
    login never blanks the other books. Snapshots are persisted per account so
    the dashboard has something to draw before the poller's next cycle.

    ``only`` restricts the fetch to a set of user ids. Pass the accounts the
    signed-in user actually manages: a page has no business spending a broker
    round-trip on somebody else's login, and the results are then filtered
    anyway.
    """
    creds = positions.credentials(db)
    if only is not None:
        creds = [c for c in creds if c[0] in only]
    if not creds:
        return {}, None
    try:
        results, fetched_at = _fetch_all_cached(tuple(creds))
    except Exception as exc:  # noqa: BLE001 - shown per page, not raised
        return ({u: {'positions': [], 'error': str(exc)} for u, _ in creds}, None)

    if st.session_state.get("_ztrade_last_fetch_all") != fetched_at:
        for user_id, res in results.items():
            if not res['error']:
                positions.save_snapshot(db, user_id, res['positions'])
        st.session_state["_ztrade_last_fetch_all"] = fetched_at
    return results, fetched_at


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
    # The instruments dump is the same for everyone, so any configured account's
    # token can fetch it — no need for the master specifically.
    creds = positions.credentials(db)
    if not creds or not symbols:
        return {}
    user_id, enctoken = creds[0]
    try:
        return _lot_sizes_cached(enctoken, user_id, symbols)
    except Exception:  # noqa: BLE001 - validation relaxes, page still works
        return {}
