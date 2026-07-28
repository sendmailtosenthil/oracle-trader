"""Login gate for the Streamlit app, with a remembered browser session.

``st.session_state`` is per-websocket, so a reload or an app restart dropped the
user back at the login form. On a successful login we now also issue a token
(see :mod:`common.session`) and store it as a browser cookie, so the login
sticks for a few days across reloads, restarts and new tabs.

Streamlit can read cookies (``st.context.cookies``) but cannot set them, so the
write goes through a zero-height HTML component that runs one line of JS
against the parent document. Because that component only executes once its
render reaches the browser, cookie writes are queued in ``session_state`` and
emitted on the following run rather than immediately before an ``st.rerun()``.
"""
import streamlit as st
import streamlit.components.v1 as components

from common.database import get_db, hash_password
from common import permissions as P
from common import session as S
from common import users as U

_PENDING = "_auth_cookie_write"   # token to persist, or "" to clear


def _queue_cookie(value):
    """Ask the next run to write (or clear, when falsy) the session cookie."""
    st.session_state[_PENDING] = value


def _flush_cookie():
    """Emit any queued cookie write. Must run on a rendered page."""
    if _PENDING not in st.session_state:
        return
    token = st.session_state.pop(_PENDING)
    if token:
        attrs = f"path=/;max-age={S.max_age_seconds()};SameSite=Lax"
        value = f"{S.COOKIE_NAME}={token};{attrs}"
    else:
        value = f"{S.COOKIE_NAME}=;path=/;max-age=0;SameSite=Lax"
    # The component runs inside an iframe; write to the parent document so the
    # cookie belongs to the app's origin and is sent back on the next request.
    components.html(
        "<script>try{parent.document.cookie=%r}catch(e){document.cookie=%r}</script>"
        % (value, value),
        height=0,
    )


def _cookie_token():
    try:
        return (st.context.cookies or {}).get(S.COOKIE_NAME)
    except Exception:  # noqa: BLE001 - no context (e.g. tests): just no cookie
        return None


def _restore_session():
    """Log the user in from a valid session cookie. True if it worked."""
    token = _cookie_token()
    if not token:
        return False
    db = next(get_db())
    try:
        username = S.resolve(db, token)
    finally:
        db.close()
    if not username:
        _queue_cookie("")  # stale or revoked — get rid of it
        return False
    st.session_state["authenticated"] = True
    st.session_state["username"] = username
    st.session_state["session_token"] = token
    return True


def require_auth():
    """Block the app until the user is logged in."""
    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False

    if not st.session_state["authenticated"] and _restore_session():
        pass  # cookie was good — fall through as authenticated

    if st.session_state["authenticated"]:
        _flush_cookie()
        return

    st.title("Project Oracle")
    st.subheader("Login")

    username = st.text_input("Username")
    password = st.text_input("Password", type="password")
    remember = st.checkbox(
        f"Keep me signed in for {S.session_days()} days", value=True,
        help="Stores a session cookie in this browser. Leave off on a shared machine.",
    )

    if st.button("Login"):
        db = next(get_db())
        try:
            # Usernames are stored lower-case, so the login box is case-blind.
            user = U.get_user(db, username)
            if user and user.password_hash == hash_password(password):
                username = user.username
                st.session_state["authenticated"] = True
                st.session_state["username"] = username
                if remember:
                    token = S.issue(db, username)
                    st.session_state["session_token"] = token
                    _queue_cookie(token)
                st.rerun()
            else:
                st.error("Invalid username or password")
        finally:
            db.close()

    _flush_cookie()   # emit a queued clear (e.g. after logout) on this page
    st.stop()         # nothing below the login form runs


def logout():
    """Log out here and forget the browser session."""
    token = st.session_state.pop("session_token", None)
    if token:
        db = next(get_db())
        try:
            S.revoke(db, token)
        finally:
            db.close()
    st.session_state["authenticated"] = False
    st.session_state.pop("username", None)
    P.clear()
    _queue_cookie("")   # cleared on the login page's render
    st.rerun()
