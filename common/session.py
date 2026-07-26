"""Remembered browser logins for the Streamlit app.

Streamlit's ``session_state`` lives and dies with the websocket, so a reload or
an app restart used to drop the user back at the login form. This issues a
long-lived random token, keeps only its SHA-256 in ``auth_sessions``, and hands
the raw token to the browser as a cookie. On the next visit the cookie is
hashed and looked up, so the login survives reloads and restarts for
``ORACLE_SESSION_DAYS`` (default 3).

Pure persistence logic — no Streamlit here. The cookie plumbing lives in
:mod:`bees.auth`.

Security notes
    * Only the hash is stored: a leaked database cannot be replayed as a login.
    * Logout deletes the row, so the token dies server-side even if the browser
      keeps the cookie.
    * The token is a bearer credential. Served over plain HTTP it is visible to
      anyone on the network path — same exposure as the password itself today,
      but it lasts for days rather than one submission. Put the app behind TLS
      before treating it as private.
"""
import datetime
import hashlib
import os
import secrets

from common.database import AuthSession

COOKIE_NAME = "oracle_session"
_DEFAULT_DAYS = 3


def session_days():
    """Lifetime of a remembered login, in days (``ORACLE_SESSION_DAYS``)."""
    try:
        days = int(os.environ.get("ORACLE_SESSION_DAYS", _DEFAULT_DAYS))
    except ValueError:
        return _DEFAULT_DAYS
    return days if days > 0 else _DEFAULT_DAYS


def max_age_seconds():
    return session_days() * 24 * 3600


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue(db, username):
    """Create a session and return the raw token (stored only as a hash)."""
    token = secrets.token_urlsafe(32)
    now = datetime.datetime.utcnow()
    db.add(AuthSession(
        token_hash=_hash(token),
        username=username,
        created_at=now,
        expires_at=now + datetime.timedelta(days=session_days()),
    ))
    db.commit()
    prune(db)
    return token


def resolve(db, token):
    """Return the username for a live token, or ``None``.

    An expired row is deleted on sight rather than left to the next prune.
    """
    if not token:
        return None
    row = db.query(AuthSession).filter(AuthSession.token_hash == _hash(token)).first()
    if row is None:
        return None
    if row.expires_at <= datetime.datetime.utcnow():
        db.delete(row)
        db.commit()
        return None
    return row.username


def revoke(db, token):
    """Drop a single session (logout). Safe to call with an unknown token."""
    if not token:
        return
    deleted = (
        db.query(AuthSession)
        .filter(AuthSession.token_hash == _hash(token))
        .delete(synchronize_session=False)
    )
    if deleted:
        db.commit()


def prune(db):
    """Delete every expired session row."""
    removed = (
        db.query(AuthSession)
        .filter(AuthSession.expires_at <= datetime.datetime.utcnow())
        .delete(synchronize_session=False)
    )
    if removed:
        db.commit()
    return removed
