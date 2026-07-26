"""Broker (Zerodha / Kite) integration helpers.

The actual Kite communication lives in :mod:`common.zerodha_client`.
This module exposes a token-validity check used across the UI. The result is
cached on the **filesystem** (a small JSON with a 1-hour TTL) rather than in RAM,
so it survives reruns without holding anything in memory and without hammering
Kite on every page load.

It also owns the **multi-account** view of ``broker_config``: several Zerodha
user ids, each with its own enctoken, can be stored side by side. See
:data:`MASTER_USER_ID` and the ``*_account`` helpers below.
"""
import hashlib
import json
import os
import tempfile
import time

from common.database import BrokerConfig
from common.zerodha_client import ZerodhaClient

_TTL = 3600  # seconds
_CACHE_FILE = os.path.join(tempfile.gettempdir(), "oracle_token_validity.json")


def _read_cache():
    try:
        with open(_CACHE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_cache(data):
    try:
        with open(_CACHE_FILE, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def _key(enctoken, user_id):
    return hashlib.sha256(f"{user_id}:{enctoken}".encode()).hexdigest()[:16]


def is_zerodha_token_valid(enctoken, user_id="PC8006"):
    """Return True if the Kite enctoken can fetch the user profile. Cached on disk
    for 1h (keyed by a hash of user_id+enctoken). Call ``clear_token_cache()``
    after saving a new token to force a recheck."""
    if not enctoken:
        return False
    key = _key(enctoken, user_id)
    now = time.time()
    cache = _read_cache()
    entry = cache.get(key)
    if entry and (now - entry.get("ts", 0)) < _TTL:
        return bool(entry.get("valid"))
    valid = ZerodhaClient(enctoken, user_id=user_id).validate()
    cache[key] = {"valid": bool(valid), "ts": now}
    _write_cache(cache)
    return valid


def clear_token_cache():
    """Invalidate the on-disk token-validity cache (after saving a new token)."""
    try:
        os.remove(_CACHE_FILE)
    except OSError:
        pass


# --- Accounts ---------------------------------------------------------------
# Several Zerodha logins can be stored at once, one ``broker_config`` row each.
#
# The **master** account (always PC8006) keeps the bare ``broker_name`` of
# 'ZERODHA'; every other account is filed under 'ZERODHA:<USER_ID>'. That layout
# is deliberate: all the automated jobs (downloader, momentum, bot, poller)
# query ``broker_name == 'ZERODHA'``, so they keep resolving to the master
# account untouched, while the extra accounts sit alongside without a schema
# change or a migration.

MASTER_USER_ID = "PC8006"
_BROKER = "ZERODHA"
_PREFIX = f"{_BROKER}:"


def normalise_user_id(user_id):
    """Kite user ids are uppercase alphanumerics — store them that way."""
    return (user_id or "").strip().upper()


def is_master(user_id):
    return normalise_user_id(user_id) == MASTER_USER_ID


def _slot(user_id):
    """The ``broker_name`` an account's row lives under."""
    uid = normalise_user_id(user_id)
    return _BROKER if is_master(uid) else f"{_PREFIX}{uid}"


def list_accounts(db):
    """Every configured Zerodha account, master first, then the rest by user id."""
    rows = (
        db.query(BrokerConfig)
        .filter(
            (BrokerConfig.broker_name == _BROKER)
            | (BrokerConfig.broker_name.startswith(_PREFIX))
        )
        .all()
    )
    rows.sort(key=lambda r: (r.broker_name != _BROKER, normalise_user_id(r.user_id)))
    return rows


def master_account(db):
    """The account every automated job uses, or None if never configured."""
    return db.query(BrokerConfig).filter(BrokerConfig.broker_name == _BROKER).first()


def get_account(db, user_id):
    """The row holding ``user_id``'s token, or None.

    Matches on ``user_id`` rather than on the slot name so a legacy master row
    that still carries some other user id is found where it actually is.
    """
    uid = normalise_user_id(user_id)
    for row in list_accounts(db):
        if normalise_user_id(row.user_id) == uid:
            return row
    return None


def _claim_master_row(db):
    """Return the row that must hold PC8006, freeing the slot if squatted.

    PC8006 is the master by rule, so it owns the bare 'ZERODHA' name. If some
    other account is sitting there (an older install that pointed the single row
    elsewhere), re-file that account under its own name first so its token is
    not lost.
    """
    row = master_account(db)
    if row is None:
        row = BrokerConfig(broker_name=_BROKER, user_id=MASTER_USER_ID, enctoken="")
        db.add(row)
        return row
    displaced = normalise_user_id(row.user_id)
    if displaced and not is_master(displaced):
        db.add(
            BrokerConfig(
                broker_name=_slot(displaced), user_id=displaced, enctoken=row.enctoken
            )
        )
    return row


def save_account(db, user_id, enctoken):
    """Create or update one account's enctoken. Commits and clears the cache.

    Raises ``ValueError`` on an empty user id or token.
    """
    uid = normalise_user_id(user_id)
    token = (enctoken or "").strip()
    if not uid:
        raise ValueError("Zerodha User ID cannot be empty.")
    if not token:
        raise ValueError("enctoken cannot be empty.")

    row = get_account(db, uid)
    if row is None:
        if is_master(uid):
            row = _claim_master_row(db)
        else:
            row = BrokerConfig(broker_name=_slot(uid), user_id=uid, enctoken=token)
            db.add(row)
    row.user_id = uid
    row.enctoken = token
    db.commit()
    clear_token_cache()
    return row


def delete_account(db, user_id):
    """Remove a non-master account. Raises ``ValueError`` for PC8006 or a miss."""
    uid = normalise_user_id(user_id)
    if is_master(uid):
        raise ValueError(f"{MASTER_USER_ID} is the master account and cannot be removed.")
    row = get_account(db, uid)
    if row is None:
        raise ValueError(f"No account configured for {uid}.")
    db.delete(row)
    db.commit()
    clear_token_cache()
