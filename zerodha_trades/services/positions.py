"""Live Kite position feed for the Zerodha Trades module.

Fetches the net positions book through the shared :class:`ZerodhaClient` and
persists it to ``ztrade_position_snapshots`` so any reader (UI, poller) can work
off the same marks without its own broker round-trip.

Open vs closed is decided purely by ``quantity``: a squared-off leg stays in
Kite's ``net`` list with ``quantity == 0`` and its P&L in ``realised``.
"""
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from common.database import BrokerConfig, PositionSnapshot
from common.zerodha_client import ZerodhaClient, fetch_with_retry


MAX_PARALLEL_ACCOUNTS = 5

# Reusable clients for the small, frequent lookups (contract detail, spot).
# Building a fresh ZerodhaClient per call means a fresh TLS handshake per call,
# which roughly doubled their latency — 196ms against 86ms on a warm session.
#
# Thread-local, not global: requests.Session is not thread-safe, and Streamlit
# serves each browser session on its own thread. The poller does not use this —
# it keeps its own long-lived client per account, one per worker.
_local = threading.local()


def lookup_client(user_id, enctoken):
    """A client for one account, reused within the calling thread.

    Named apart from ``fetch_many``'s ``client_for`` parameter, which is the
    poller's own per-worker factory and would otherwise shadow this.
    """
    clients = getattr(_local, 'clients', None)
    if clients is None:
        clients = _local.clients = {}
    key = (user_id, enctoken)
    if key not in clients:
        # A rotated token makes the old session useless; drop it rather than
        # accumulate one dead client per refresh.
        for stale in [k for k in clients if k[0] == user_id]:
            clients.pop(stale, None)
        clients[key] = ZerodhaClient(enctoken, user_id=user_id or 'PC8006',
                                     pace_seconds=0)
    return clients[key]


def broker_credentials(db):
    """Return ``(enctoken, user_id)`` for the master account, or ``(None, None)``."""
    cfg = db.query(BrokerConfig).filter(BrokerConfig.broker_name == 'ZERODHA').first()
    if not cfg or not cfg.enctoken:
        return None, None
    return cfg.enctoken, cfg.user_id


def credentials(db):
    """``[(user_id, enctoken), ...]`` for every configured account, master first.

    Accounts without a token are skipped — there is nothing to fetch with.
    """
    from common.broker import list_accounts, normalise_user_id
    return [
        (normalise_user_id(row.user_id), row.enctoken)
        for row in list_accounts(db)
        if row.enctoken
    ]


def fetch_many(creds, max_workers=MAX_PARALLEL_ACCOUNTS, client_for=None):
    """Fetch several accounts' books concurrently.

    Returns ``{user_id: {'positions': [...], 'error': str|None}}``. One account
    failing — an expired token, say — never sinks the others; its entry carries
    the message instead.

    Concurrency is capped at ``max_workers`` (5 by default): the pool runs that
    many at a time and queues the rest, so twenty accounts still means five
    sockets open at once rather than twenty.
    """
    creds = [(u, t) for u, t in creds if u and t]
    if not creds:
        return {}
    if len(creds) == 1:                       # no pool for the common case
        user_id, token = creds[0]
        return {user_id: _fetch_one(user_id, token, client_for)}

    out = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(creds)),
                            thread_name_prefix="ztrade-fetch") as pool:
        futures = {pool.submit(_fetch_one, u, t, client_for): u for u, t in creds}
        for future in as_completed(futures):
            user_id = futures[future]
            try:
                out[user_id] = future.result()
            except Exception as exc:  # noqa: BLE001 - reported per account
                out[user_id] = {'positions': [], 'error': str(exc)}
    return out


def _fetch_one(user_id, enctoken, client_for=None):
    try:
        if client_for is None:
            return {'positions': fetch(enctoken, user_id), 'error': None}
        # Caller-supplied client: one long-lived session per account, reused
        # across poll cycles. Safe because each account's client is only ever
        # touched by the one worker handling that account.
        client = client_for(user_id, enctoken)
        rows = fetch_with_retry(client.get_positions)
        return {'positions': [normalize(r) for r in rows], 'error': None}
    except Exception as exc:  # noqa: BLE001 - surfaced against this account
        return {'positions': [], 'error': str(exc)}


def normalize(row):
    """Flatten one raw Kite position row to the fields this module uses."""
    return {
        'tradingsymbol': row.get('tradingsymbol') or '',
        'exchange': row.get('exchange') or '',
        'product': row.get('product') or '',
        'instrument_token': int(row.get('instrument_token') or 0),
        'quantity': int(row.get('quantity') or 0),
        'average_price': float(row.get('average_price') or 0.0),
        'last_price': float(row.get('last_price') or 0.0),
        'pnl': float(row.get('pnl') or 0.0),
        'realised': float(row.get('realised') or 0.0),
        'unrealised': float(row.get('unrealised') or 0.0),
        'basis_quantity': _basis_quantity(row),
    }


def _basis_quantity(row):
    """Signed size to tag a group against.

    For an open position that is simply its quantity. A squared-off one
    reports ``quantity == 0``, so fall back to the size it *had* — that is
    what a group holding it was ever a share of, and without it a closed
    position could not be added to a group at all.
    """
    quantity = int(row.get('quantity') or 0)
    if quantity:
        return quantity
    overnight = int(row.get('overnight_quantity') or 0)
    if overnight:
        return overnight
    # Opened and closed within the day: size is whichever side filled,
    # and the sign says which came first.
    bought = int(row.get('buy_quantity') or 0)
    sold = int(row.get('sell_quantity') or 0)
    size = max(abs(bought), abs(sold))
    return -size if sold > bought else size


def fetch(enctoken, user_id):
    """Fetch and normalize the live net position book.

    Takes raw credentials (not a session) so callers can memoise on them.
    Raises ``RuntimeError`` / ``FatalAuthError`` on broker failure.
    """
    if not enctoken:
        raise RuntimeError("No Zerodha enctoken configured — set one in Broker Setup.")
    client = ZerodhaClient(enctoken, user_id=user_id or 'PC8006', pace_seconds=0)
    return [normalize(r) for r in fetch_with_retry(client.get_positions)]


def fetch_for(db):
    """Convenience wrapper: read credentials from the DB, then :func:`fetch`."""
    enctoken, user_id = broker_credentials(db)
    return fetch(enctoken, user_id)


def contracts(enctoken, user_id, tradingsymbols):
    """``{tradingsymbol: contract}`` — strike, expiry, type and lot size.

    What the payoff diagram needs to price a leg, straight off the instruments
    master. See :meth:`ZerodhaClient.contract_map`.
    """
    if not enctoken or not tradingsymbols:
        return {}
    client = lookup_client(user_id, enctoken)
    return fetch_with_retry(lambda: client.contract_map(tradingsymbols))


def spot_tokens(enctoken, user_id, names):
    """``{underlying_name: instrument_token}`` for the things derivatives track."""
    if not enctoken or not names:
        return {}
    client = lookup_client(user_id, enctoken)
    resolved = {n: fetch_with_retry(lambda n=n: client.spot_token(n)) for n in names}
    return {n: t for n, t in resolved.items() if t}


def spot_price(enctoken, user_id, instrument_token):
    """Latest traded price of an underlying — see ``ZerodhaClient.last_traded_price``."""
    if not enctoken or not instrument_token:
        return None
    client = lookup_client(user_id, enctoken)
    return fetch_with_retry(lambda: client.last_traded_price(instrument_token))


def open_only(positions):
    """Only the legs still open (non-zero quantity)."""
    return [p for p in positions if p['quantity'] != 0]


def position_key(tradingsymbol, product):
    """Identity of a position across polls: symbol + product bucket."""
    return (tradingsymbol, product)


def as_map(positions):
    """Index normalized positions by :func:`position_key`."""
    return {position_key(p['tradingsymbol'], p['product']): p for p in positions}


def save_snapshot(db, user_id, positions, commit=True):
    """Upsert one account's polled book into ``ztrade_position_snapshots``.

    Scoped to ``user_id``: saving one account never touches another's rows.
    Rows the broker no longer reports are left untouched — a leg that vanishes
    is handled by the group marking code, which freezes its P&L rather than
    silently valuing it at zero.

    ``commit=False`` leaves the rows pending so a caller saving several accounts
    can land them in one transaction instead of one fsync each. Nothing
    downstream reads these rows back within a cycle — marking works off the
    fetched book, not the table — so deferring is safe.
    """
    now = datetime.datetime.utcnow()
    existing = {
        position_key(r.tradingsymbol, r.product): r
        for r in db.query(PositionSnapshot).filter(PositionSnapshot.user_id == user_id)
    }
    for p in positions:
        row = existing.get(position_key(p['tradingsymbol'], p['product']))
        if row is None:
            row = PositionSnapshot(user_id=user_id, tradingsymbol=p['tradingsymbol'],
                                   product=p['product'])
            db.add(row)
        row.exchange = p['exchange']
        row.instrument_token = p['instrument_token']
        row.quantity = p['quantity']
        row.average_price = p['average_price']
        row.last_price = p['last_price']
        row.pnl = p['pnl']
        row.realised = p['realised']
        row.unrealised = p['unrealised']
        row.updated_at = now
    if commit:
        db.commit()
    return len(positions)


def load_snapshot(db, user_id=None):
    """Read persisted positions back as normalized dicts.

    ``user_id`` narrows to one account; omit it for every account (each dict
    carries its own ``user_id``).
    """
    query = db.query(PositionSnapshot)
    if user_id is not None:
        query = query.filter(PositionSnapshot.user_id == user_id)
    rows = query.order_by(PositionSnapshot.tradingsymbol).all()
    return [
        {
            'user_id': r.user_id,
            'tradingsymbol': r.tradingsymbol,
            'exchange': r.exchange,
            'product': r.product,
            'instrument_token': r.instrument_token,
            'quantity': r.quantity,
            'average_price': r.average_price,
            'last_price': r.last_price,
            'pnl': r.pnl,
            'realised': r.realised,
            'unrealised': r.unrealised,
            'updated_at': r.updated_at,
        }
        for r in rows
    ]


def snapshot_age(db, user_id=None):
    """Newest snapshot timestamp (UTC), or ``None`` if nothing polled yet."""
    query = db.query(PositionSnapshot)
    if user_id is not None:
        query = query.filter(PositionSnapshot.user_id == user_id)
    row = query.order_by(PositionSnapshot.updated_at.desc()).first()
    return row.updated_at if row else None


def snapshot_maps(db):
    """``{user_id: {(symbol, product): position}}`` across every account."""
    out = {}
    for row in load_snapshot(db):
        out.setdefault(row['user_id'], {})[
            position_key(row['tradingsymbol'], row['product'])] = row
    return out
