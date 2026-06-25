"""Broker (Zerodha / Kite) integration helpers.

The actual Kite communication lives in :mod:`common.zerodha_client`.
This module exposes a token-validity check used across the UI. The result is
cached on the **filesystem** (a small JSON with a 1-hour TTL) rather than in RAM,
so it survives reruns without holding anything in memory and without hammering
Kite on every page load.
"""
import hashlib
import json
import os
import tempfile
import time

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
