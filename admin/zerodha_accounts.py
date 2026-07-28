"""Zerodha Accounts — add Kite logins and keep their credentials.

Each account belongs to the app user who added it. Everyone who can open this
page sees that an account exists (its Kite user id, who owns it, whether its
token is live) — that is what makes the Trades module legible across a team.
Nobody but the owner and administrators can see or change its password and TOTP
secret, or remove it.

Those two secrets are what the 8:10am headless login uses to mint a fresh
enctoken without anyone present. They are encrypted at rest with a key held
outside the database (see :mod:`common.secrets`), because the nightly backup
uploads the database itself to Google Drive.
"""
import streamlit as st

from common import permissions as P
from common import secrets as sec
from common.broker import (
    MASTER_USER_ID,
    can_manage,
    delete_account,
    get_account,
    has_credentials,
    is_master,
    is_zerodha_token_valid,
    normalise_user_id,
    owner_of,
    save_account,
    set_credentials,
    visible_accounts,
)

PAGE = "setup.zerodha"


def _token_badge(account):
    if not account.enctoken:
        return "⚪ no token"
    return ("✅ token valid" if is_zerodha_token_valid(account.enctoken, account.user_id)
            else "❌ token expired")


def _owner_badge(account):
    owner = owner_of(account)
    if not owner:
        return "🟠 unclaimed"
    return f"👤 {owner}{' (you)' if owner == P.current_user() else ''}"


def _totp_now(secret):
    """The current 6-digit code, so the owner can check the secret is right."""
    from common.zerodha_login import _totp
    try:
        return _totp(secret)
    except Exception:  # noqa: BLE001 - a bad base32 secret is the whole point
        return None


def _crypto_warning():
    if sec.available():
        return False
    st.error(
        "🔐 **Credential storage is unavailable.** " + sec.install_hint()
        + "  \nAccounts and enctokens still work; passwords and TOTP secrets "
          "cannot be saved until then — they would otherwise be written to a "
          "database that is uploaded to Google Drive every night."
    )
    return True


def _overview(accounts):
    st.subheader(f"Accounts ({len(accounts)})")
    st.dataframe(
        [{
            "Zerodha ID": normalise_user_id(a.user_id),
            "Role": "master" if is_master(a.user_id) else "additional",
            "Added by": owner_of(a) or "—",
            "Token": _token_badge(a),
            "Auto-login": "✅ stored" if has_credentials(a) else "—",
        } for a in accounts],
        hide_index=True,
        width='stretch',
        column_config={
            "Role": st.column_config.TextColumn(
                "Role", help=f"{MASTER_USER_ID} is the master account every "
                             "automated job runs on."),
            "Added by": st.column_config.TextColumn(
                "Added by", help="Only this user (and administrators) can see or "
                                 "change the account's password and TOTP secret."),
            "Auto-login": st.column_config.TextColumn(
                "Auto-login", help="Whether a password + TOTP secret are stored, "
                                   "so the 8:10am job can refresh the enctoken "
                                   "on its own."),
        },
    )
    st.caption("Everyone with access to this page can see the list. Passwords and "
               "TOTP secrets are only ever shown to the user who added the account.")


def _credentials_form(db, account, uid):
    """Password / TOTP editor — only ever rendered for an account you manage."""
    password, totp = (sec.decrypt(account.password_enc), sec.decrypt(account.totp_enc))

    unreadable = [name for name, val in (("password", password), ("TOTP secret", totp))
                  if val is None]
    if unreadable:
        st.warning(
            f"The stored {' and '.join(unreadable)} can't be decrypted on this host — "
            "the encryption key changed or is missing (`data/secret.key`). "
            "Re-enter below to replace them."
        )

    with st.form(f"ztd_creds_{uid}"):
        st.markdown("**Automatic login** — used by the 8:10am enctoken refresh.")
        new_pw = st.text_input(
            "Kite password", value=password or "", type="password",
            key=f"ztd_pw_{uid}", help="Leave blank to clear the stored password.")
        new_totp = st.text_input(
            "TOTP secret (base32)", value=totp or "", type="password",
            key=f"ztd_totp_{uid}",
            help="The 'external 2FA app' key from Kite — not the 6-digit code.")
        if st.form_submit_button("Save credentials", type="primary"):
            try:
                set_credentials(db, uid, password=new_pw, totp_secret=new_totp,
                                owner=P.current_user())
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success(f"Credentials saved for {uid}.")
                st.rerun()

    if totp:
        code = _totp_now(totp)
        if code:
            st.caption(f"🔢 Current TOTP for {uid}: **{code}** — matches your "
                       "authenticator app if the secret is right.")
        else:
            st.warning("That TOTP secret isn't valid base32 — the automatic login "
                       "will fail. Copy the key Kite showed when you enabled 2FA.")


def _account_panel(db, account):
    uid = normalise_user_id(account.user_id)
    # Two separate gates: the page level says whether you may change anything at
    # all, ownership says which accounts. Secrets follow the same rule — a
    # read-only visitor never sees them, not even for their own account.
    mine = can_manage(account) and P.can_edit(PAGE)
    header = f"{'🔧' if mine else '🔒'} {uid} · {_token_badge(account)} · {_owner_badge(account)}"

    with st.expander(header, expanded=False):
        if not mine:
            owner = owner_of(account)
            if can_manage(account):
                st.info(f"**{uid}** is yours, but your access to this page is "
                        "read-only, so its credentials stay hidden.")
            else:
                st.info(
                    f"**{uid}** was added by **{owner or 'nobody'}**. You can see "
                    "that it exists, but its password, TOTP secret and enctoken "
                    "belong to them."
                )
            return

        with st.form(f"ztd_token_{uid}"):
            enctoken = st.text_input("Kite enctoken", value=account.enctoken or "",
                                     type="password", key=f"ztd_enc_{uid}")
            if st.form_submit_button("Save enctoken"):
                try:
                    save_account(db, uid, enctoken, owner=P.current_user())
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.success(f"enctoken saved for {uid}.")
                    st.rerun()

        if not _crypto_warning():
            _credentials_form(db, account, uid)

        if is_master(uid):
            st.caption(f"{MASTER_USER_ID} is the master account — every automated "
                       "job runs on it, so it can't be removed.")
        else:
            st.markdown("**Remove**")
            confirm = st.checkbox(f"Yes, remove {uid} and its stored credentials",
                                  key=f"ztd_del_{uid}")
            if st.button("Remove account", key=f"ztd_delbtn_{uid}", disabled=not confirm):
                try:
                    delete_account(db, uid)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.success(f"{uid} removed.")
                    st.rerun()


def _add_form(db):
    with st.expander("➕ Add a Zerodha account", expanded=False):
        st.caption(
            "The account is filed under your name: only you (and administrators) "
            "will be able to see or change its credentials, and its positions and "
            "groups appear under your Zerodha Trades tabs."
        )
        crypto_missing = _crypto_warning()

        new_uid = st.text_input("Zerodha User ID", placeholder="e.g. AB1234",
                                key="ztd_new_uid")
        c1, c2 = st.columns(2)
        new_pw = c1.text_input("Kite password", type="password", key="ztd_new_pw",
                               disabled=crypto_missing)
        new_totp = c2.text_input("TOTP secret (base32)", type="password",
                                 key="ztd_new_totp", disabled=crypto_missing,
                                 help="Optional. Needed for the automatic 8:10am "
                                      "enctoken refresh.")
        new_enc = st.text_input(
            "Kite enctoken", type="password", key="ztd_new_enc",
            help="Optional — leave blank if you'll push it from the browser "
                 "extension or let the automatic login fetch it.")

        if st.button("Add account", type="primary", key="ztd_add"):
            uid = normalise_user_id(new_uid)
            if not uid:
                st.error("Zerodha User ID cannot be empty.")
                return
            if get_account(db, uid):
                st.error(f"{uid} is already configured — open it above.")
                return
            try:
                set_credentials(db, uid, password=new_pw, totp_secret=new_totp,
                                owner=P.current_user())
                if (new_enc or "").strip():
                    save_account(db, uid, new_enc, owner=P.current_user())
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success(f"Added {uid} under your name.")
                st.rerun()


def render(db):
    P.guard(PAGE)
    st.title("📇 Zerodha Accounts")
    st.write(
        "Kite logins used across the app. An account belongs to whoever adds it: "
        "its password and TOTP secret are visible only to them, and its positions "
        "and trade groups appear only under their Zerodha Trades tabs."
    )

    accounts = visible_accounts(db)
    if not accounts:
        st.info("No Zerodha accounts configured yet — add one below.")
    else:
        _overview(accounts)
        st.divider()

    if P.can_edit(PAGE):
        _add_form(db)
    else:
        P.readonly_note(PAGE, "the configured accounts")

    for account in accounts:
        _account_panel(db, account)
