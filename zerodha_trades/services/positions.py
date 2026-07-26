"""Live Kite position feed for the Zerodha Trades module.

Fetches the net positions book through the shared :class:`ZerodhaClient` and
persists it to ``ztrade_position_snapshots`` so any reader (UI, poller) can work
off the same marks without its own broker round-trip.

Open vs closed is decided purely by ``quantity``: a squared-off leg stays in
Kite's ``net`` list with ``quantity == 0`` and its P&L in ``realised``.
"""
import datetime

from common.database import BrokerConfig, PositionSnapshot
from common.zerodha_client import ZerodhaClient, fetch_with_retry


def broker_credentials(db):
    """Return ``(enctoken, user_id)`` from broker_config, or ``(None, None)``."""
    cfg = db.query(BrokerConfig).filter(BrokerConfig.broker_name == 'ZERODHA').first()
    if not cfg or not cfg.enctoken:
        return None, None
    return cfg.enctoken, cfg.user_id


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
    }


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


def lot_sizes(enctoken, user_id, tradingsymbols):
    """``{tradingsymbol: lot_size}`` for the given symbols (F&O contract size)."""
    if not enctoken or not tradingsymbols:
        return {}
    client = ZerodhaClient(enctoken, user_id=user_id or 'PC8006', pace_seconds=0)
    return fetch_with_retry(lambda: client.lot_size_map(tradingsymbols))


def open_only(positions):
    """Only the legs still open (non-zero quantity)."""
    return [p for p in positions if p['quantity'] != 0]


def position_key(tradingsymbol, product):
    """Identity of a position across polls: symbol + product bucket."""
    return (tradingsymbol, product)


def as_map(positions):
    """Index normalized positions by :func:`position_key`."""
    return {position_key(p['tradingsymbol'], p['product']): p for p in positions}


def save_snapshot(db, positions):
    """Upsert the polled book into ``ztrade_position_snapshots``.

    Rows the broker no longer reports at all are left untouched — a leg that
    vanishes is handled by the group marking code, which freezes its P&L rather
    than silently valuing it at zero.
    """
    now = datetime.datetime.utcnow()
    existing = {
        position_key(r.tradingsymbol, r.product): r
        for r in db.query(PositionSnapshot).all()
    }
    for p in positions:
        row = existing.get(position_key(p['tradingsymbol'], p['product']))
        if row is None:
            row = PositionSnapshot(tradingsymbol=p['tradingsymbol'], product=p['product'])
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
    db.commit()
    return len(positions)


def load_snapshot(db):
    """Read the last persisted book back as normalized dicts."""
    rows = db.query(PositionSnapshot).order_by(PositionSnapshot.tradingsymbol).all()
    return [
        {
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


def snapshot_age(db):
    """Newest snapshot timestamp (UTC), or ``None`` if nothing polled yet."""
    row = (
        db.query(PositionSnapshot)
        .order_by(PositionSnapshot.updated_at.desc())
        .first()
    )
    return row.updated_at if row else None
