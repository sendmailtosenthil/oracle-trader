"""User Management page — create logins and grant them access, page by page.

Administrators only. The access grid is generated from the registry in
:mod:`common.permissions`, so a page added there shows up here automatically.
"""
import streamlit as st

from common import permissions as P
from common import users as U
from common.timez import to_ist


def _level_picker(page, current, key):
    """One radio row: the levels this page actually supports."""
    labels = {lvl: P.LEVEL_LABELS[lvl] for lvl in page.levels}
    options = list(page.levels)
    index = options.index(current) if current in options else 0
    return st.radio(
        f"**{page.title}**", options, index=index, horizontal=True,
        format_func=lambda lvl: labels[lvl], key=key,
        help=page.help + ("" if page.readable else "  \n_Editing page — no read-only mode._"),
    )


def _access_grid(prefix, current):
    """Render the whole permission grid; return the chosen ``{key: level}``."""
    chosen = {}
    for section in P.SECTIONS:
        pages = [p for p in P.pages_in(section) if not p.admin_only]
        if not pages:
            continue
        st.markdown(f"**{section}**")
        for page in pages:
            chosen[page.key] = _level_picker(
                page, current.get(page.key, P.NONE), f"{prefix}_{page.key}"
            )
        st.divider()
    return chosen


def _create_user(db):
    with st.expander("➕ Add a user", expanded=False):
        username = st.text_input("Username", key="newuser_name",
                                 placeholder="lowercase, e.g. sasi")
        c1, c2 = st.columns(2)
        password = c1.text_input("Password", type="password", key="newuser_pw")
        confirm = c2.text_input("Confirm password", type="password", key="newuser_pw2")
        is_admin = st.checkbox(
            "Administrator (full access to every page, including this one)",
            key="newuser_admin",
        )

        if not is_admin:
            st.markdown("##### Access")
            perms = _access_grid("newuser", P.empty())
        else:
            perms = {}
            st.info("Administrators get every page — no per-page grants needed.")

        if st.button("Create user", type="primary", key="newuser_create"):
            _, err = U.create_user(db, username, password, confirm, perms, is_admin)
            if err:
                st.error(err)
            else:
                st.success(f"Created '{U.normalise_username(username)}'.")
                st.rerun()


def _edit_user(db, user, me):
    is_self = user.username == me
    created = to_ist(user.created_at).strftime("%d %b %Y") if user.created_at else "—"
    badge = " · 👑 administrator" if user.is_admin else ""
    title = f"👤 **{user.username}**{badge}{' · you' if is_self else ''}"

    with st.expander(title, expanded=False):
        st.caption(f"Created {created} · {P.summarise(user)}")

        st.markdown("##### Access")
        admin_flag = st.checkbox(
            "Administrator", value=bool(user.is_admin), key=f"admin_{user.id}",
            help="Full access to every page, including User Management.",
        )
        if admin_flag:
            perms = P.loads(user.permissions)
            st.info("Administrators get every page — per-page grants are ignored.")
        else:
            perms = _access_grid(f"perm_{user.id}", P.for_user(user))

        if st.button("Save access", type="primary", key=f"save_{user.id}"):
            _, err = U.set_permissions(db, user, perms, is_admin=admin_flag)
            if err:
                st.error(err)
            else:
                st.success(f"Access updated for '{user.username}'.")
                st.rerun()

        st.markdown("##### Password")
        st.caption("Changing the password signs this user out of every browser.")
        p1, p2 = st.columns(2)
        pw = p1.text_input("New password", type="password", key=f"pw_{user.id}")
        pw2 = p2.text_input("Confirm", type="password", key=f"pw2_{user.id}")
        if st.button("Set password", key=f"setpw_{user.id}"):
            _, err = U.set_password(db, user, pw, pw2)
            if err:
                st.error(err)
            else:
                st.success(f"Password changed for '{user.username}'.")
                st.rerun()

        st.markdown("##### Remove")
        if is_self:
            st.caption("You can't delete the account you're signed in with.")
        else:
            confirm = st.checkbox(f"Yes, delete '{user.username}'", key=f"del_{user.id}")
            if st.button("Delete user", key=f"delbtn_{user.id}", disabled=not confirm):
                _, err = U.delete_user(db, user, acting_username=me)
                if err:
                    st.error(err)
                else:
                    st.success(f"Deleted '{user.username}'.")
                    st.rerun()


def render(db):
    st.title("👥 User Management")

    if not P.is_admin():
        st.error("Administrators only.")
        st.stop()

    st.write(
        "Logins are stored in the database and managed here. Each user gets a "
        "level per page: no access, read only, or edit. Pages that exist purely "
        "to change things offer no read-only mode."
    )

    me = st.session_state.get("username", "")
    users = U.list_users(db)

    _create_user(db)
    st.divider()

    st.subheader(f"Users ({len(users)})")
    for user in users:
        _edit_user(db, user, me)
