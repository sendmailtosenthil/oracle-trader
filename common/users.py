"""Creating, editing and removing logins.

Kept free of Streamlit so both the User Management page and
``scripts/manage_users.py`` (the way back in when nobody can log in) share one
set of rules:

* usernames are lower-cased and unique;
* the last administrator can neither be demoted nor deleted, so the app can
  never end up with no one able to manage it;
* changing a password or deleting a user drops that user's remembered browser
  sessions, so a revoked login stops working immediately rather than at the end
  of the cookie's life.

Every function returns ``(value, error)`` — ``error`` is a message fit to show
the user, and ``value`` is ``None`` when it is set.
"""
import re

from common.database import AuthSession, User, hash_password
from common import permissions as P

MIN_PASSWORD_LEN = 8
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")


def normalise_username(username):
    return (username or "").strip().lower()


def list_users(db):
    return db.query(User).order_by(User.username.asc()).all()


def get_user(db, username):
    return db.query(User).filter(User.username == normalise_username(username)).first()


def admin_count(db, excluding=None):
    q = db.query(User).filter(User.is_admin.is_(True))
    if excluding is not None:
        q = q.filter(User.id != excluding)
    return q.count()


def _check_username(db, username):
    if not _USERNAME_RE.match(username):
        return ("Username must be 2–32 characters: lowercase letters, digits, "
                "dot, dash or underscore, starting with a letter or digit.")
    if get_user(db, username):
        return f"'{username}' already exists."
    return None


def _check_password(password, confirm=None):
    if len(password or "") < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if confirm is not None and password != confirm:
        return "The two passwords don't match."
    return None


def revoke_sessions(db, username):
    """Drop every remembered browser session belonging to a user."""
    removed = (
        db.query(AuthSession)
        .filter(AuthSession.username == normalise_username(username))
        .delete(synchronize_session=False)
    )
    if removed:
        db.commit()
    return removed


def create_user(db, username, password, confirm=None, permissions=None, is_admin=False):
    username = normalise_username(username)
    err = _check_username(db, username) or _check_password(password, confirm)
    if err:
        return None, err

    user = User(
        username=username,
        password_hash=hash_password(password),
        is_admin=bool(is_admin),
        permissions=P.dumps({} if is_admin else (permissions or {})),
    )
    db.add(user)
    db.commit()
    return user, None


def set_permissions(db, user, permissions, is_admin=None):
    if is_admin is not None:
        is_admin = bool(is_admin)
        if user.is_admin and not is_admin and admin_count(db, excluding=user.id) == 0:
            return None, ("This is the last administrator — promote someone else "
                          "before removing the flag.")
        user.is_admin = is_admin

    # An admin's map is meaningless (they get everything); store an empty one so
    # demoting later starts from no access rather than a stale grant.
    user.permissions = P.dumps({} if user.is_admin else (permissions or {}))
    db.commit()
    return user, None


def set_password(db, user, password, confirm=None):
    err = _check_password(password, confirm)
    if err:
        return None, err
    user.password_hash = hash_password(password)
    db.commit()
    revoke_sessions(db, user.username)
    return user, None


def delete_user(db, user, acting_username=None):
    if acting_username and normalise_username(acting_username) == user.username:
        return None, "You can't delete the account you're signed in with."
    if user.is_admin and admin_count(db, excluding=user.id) == 0:
        return None, "This is the last administrator — the app would be unmanageable."
    username = user.username
    db.delete(user)
    db.commit()
    revoke_sessions(db, username)
    return username, None
