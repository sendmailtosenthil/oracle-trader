"""Shared view helpers for the Zerodha Trades pages."""
import datetime

import pytz
import streamlit as st

from zerodha_trades.services import payoff
from zerodha_trades.services import positions

IST = pytz.timezone("Asia/Kolkata")

# How often a live view re-marks itself. Matches the poller's default cycle —
# anything faster just redraws the same snapshot.
LIVE_SECONDS = 10

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


# Contract detail only changes when the exchange revises an instrument, so an
# hours-long TTL is plenty.
#
# Accumulated in one store rather than cached per requested symbol set. Keying on
# the set gave Group Management (every symbol on the page) and each group's
# payoff view (just its own legs) separate entries, so once there were more
# distinct sets than `max_entries` they evicted each other and re-streamed the
# whole 113,800-row instruments master — from inside a fragment that reruns every
# ten seconds. Accumulating means a symbol is looked up once and every later
# caller, whatever set it asks for, is free.
@st.cache_resource(ttl=21600, show_spinner=False)
def _contract_store():
    """``{tradingsymbol: contract | None}``, shared process-wide.

    ``None`` records "looked this up, the exchange does not list it", so a
    delisted or mistyped symbol is not re-fetched on every rerun. Concurrent
    reruns may duplicate a fetch, which costs a little work and nothing else.
    """
    return {}


def _any_account(db):
    """Credentials of any configured account, or ``(None, None)``.

    The instruments dump and the quote feed are the same for everyone, so
    whichever token is to hand will do — no need for the master specifically.
    """
    creds = positions.credentials(db)
    return creds[0] if creds else (None, None)


def contracts(db, symbols):
    """``{tradingsymbol: contract}`` from the instruments master, or ``{}``.

    Only the symbols never looked up before cost anything; the rest come out of
    :func:`_contract_store`. Degrading to an empty map is deliberate: callers
    relax rather than block the user on a broker hiccup.
    """
    wanted = {s for s in symbols if s}
    if not wanted:
        return {}
    store = _contract_store()
    unknown = tuple(sorted(wanted - store.keys()))
    if unknown:
        user_id, enctoken = _any_account(db)
        found = None
        if enctoken:
            try:
                found = positions.contracts(enctoken, user_id, unknown)
            except Exception:  # noqa: BLE001 - callers relax, page still works
                found = None
        # Only record absences when the lookup actually succeeded. Marking them
        # on a failed fetch would remember "not listed" for the whole TTL and
        # leave the chart permanently unable to price the group.
        if found is not None:
            store.update({s: found.get(s) for s in unknown})
    return {s: c for s in wanted if (c := store.get(s))}


def lot_sizes(db, symbols):
    """``{tradingsymbol: lot_size}``, or ``{}`` if they can't be resolved.

    Degrading to an empty map is deliberate: quantity validation then skips the
    whole-lots rule rather than blocking the user on a broker hiccup.
    """
    return {s: c['lot_size'] for s, c in contracts(db, symbols).items()}


# An underlying's spot token never changes, so it is cached as long as the
# contract detail it comes from.
@st.cache_data(ttl=21600, max_entries=4, show_spinner=False)
def _spot_tokens_cached(enctoken, user_id, names):
    return positions.spot_tokens(enctoken, user_id, names)


# The price behind it does change, so it is cached only as long as the live
# refresh — one small call per underlying per cycle, however many charts are
# open on it.
@st.cache_data(ttl=LIVE_SECONDS, max_entries=8, show_spinner=False)
def _spot_price_cached(enctoken, user_id, instrument_token):
    return positions.spot_price(enctoken, user_id, instrument_token)


def spot_price(db, name):
    """Last traded price of the underlying ``name`` tracks, or ``None``.

    Degrades to ``None`` rather than raising; :func:`underlying_spot` has the
    fallbacks.
    """
    user_id, enctoken = _any_account(db)
    if not enctoken or not name:
        return None
    try:
        token = _spot_tokens_cached(enctoken, user_id, (name,)).get(name)
        return _spot_price_cached(enctoken, user_id, token) if token else None
    except Exception:  # noqa: BLE001 - the caller falls back and says so
        return None


def underlying_spot(db, items, contracts, name):
    """``(spot, source)`` for an underlying, best source first.

    The index or stock's own last print, else whatever the group's own marks
    imply (see :func:`payoff.spot_from_book`). One place, so the chart and the
    deploy-time baseline can never disagree about where the underlying was —
    which would make the reference band meaningless.
    """
    price = spot_price(db, name)
    if price:
        return price, f"{name} last traded"
    return payoff.spot_from_book(items, contracts, name)
