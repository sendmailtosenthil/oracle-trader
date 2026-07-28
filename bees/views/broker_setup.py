"""Broker Setup page: the enctoken for each Zerodha login you added.

Accounts themselves — adding them, their password and TOTP secret — live on
**Setup › Zerodha Accounts**. This page stays focused on the one value that
expires daily and gets pasted in by hand or pushed by the browser extension.

The same ownership rule applies in both places, and for the same reason: an
enctoken is a live session. Only the user who added an account (and
administrators) can read or replace its token; everyone else sees that the
account exists and whether its token is healthy.
"""
import streamlit as st

from common.broker import (
    MASTER_USER_ID,
    can_manage,
    delete_account,
    get_account,
    is_zerodha_token_valid,
    list_accounts,
    master_account,
    normalise_user_id,
    owner_of,
    save_account,
)
from common import permissions as P

PAGE = "setup.broker"


def _status(account):
    """A one-glance validity badge for an account's stored token."""
    if not account or not account.enctoken:
        return "⚪ no token saved"
    if is_zerodha_token_valid(account.enctoken, account.user_id):
        return "✅ token valid"
    return "❌ token expired or invalid"


def _owner_note(account):
    owner = owner_of(account)
    if not owner:
        return "🟠 unclaimed — an administrator can assign it on **Zerodha Accounts**"
    return f"👤 added by {owner}{' (you)' if owner == P.current_user() else ''}"


def _locked(account, uid):
    """Show the read-only view of somebody else's account."""
    st.write(f"{_status(account)} · {_owner_note(account)}")
    st.caption(f"Its enctoken belongs to **{owner_of(account) or 'nobody'}** — "
               f"ask them to refresh {uid}, or use an account of your own.")


def render(db):
    P.guard(PAGE)
    st.title("Broker Setup & Integrations")
    st.write("Paste a fresh Kite enctoken for the accounts you added. Add or "
             "remove accounts on **Setup › Zerodha Accounts**.")

    st.subheader("Zerodha / Kite")

    master = master_account(db)
    others = [a for a in list_accounts(db) if a is not master]

    st.markdown(f"#### Master account — `{MASTER_USER_ID}`")
    st.caption(
        "Used by every automated job: downloader, momentum, the bot and the "
        "position poller. Its User ID is fixed."
    )

    if master is None:
        st.info(f"{MASTER_USER_ID} is not configured yet — add it on "
                "**Setup › Zerodha Accounts**.")
    elif not can_manage(master):
        _locked(master, MASTER_USER_ID)
    else:
        st.write(f"{_status(master)} · {_owner_note(master)}")
        with st.form(key="zerodha_master_form"):
            st.text_input("Zerodha User ID", value=MASTER_USER_ID, disabled=True)
            m_enctoken = st.text_input(
                "Kite enctoken", value=master.enctoken, type="password"
            )
            if st.form_submit_button("Save master enctoken", type="primary"):
                try:
                    save_account(db, MASTER_USER_ID, m_enctoken, owner=P.current_user())
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.success(f"{MASTER_USER_ID} credentials saved successfully!")
                    st.rerun()

    st.divider()
    st.markdown("#### Additional accounts")
    st.caption(
        "Extra Zerodha logins kept alongside the master. Their tokens are stored "
        "and validated here; automated jobs continue to run on "
        f"`{MASTER_USER_ID}` only."
    )

    if not others:
        st.info("No additional accounts yet — add one on **Setup › Zerodha Accounts**.")

    for account in others:
        uid = normalise_user_id(account.user_id)
        with st.expander(f"{uid} — {_status(account)}"):
            if not can_manage(account):
                _locked(account, uid)
                continue

            st.caption(_owner_note(account))
            with st.form(key=f"zerodha_account_{uid}"):
                enctoken = st.text_input(
                    "Kite enctoken", value=account.enctoken, type="password",
                    key=f"enctoken_{uid}",
                )
                save_col, remove_col = st.columns([1, 1])
                saved = save_col.form_submit_button("Save enctoken", type="primary")
                removed = remove_col.form_submit_button(f"Remove {uid}")

            if saved:
                try:
                    save_account(db, uid, enctoken, owner=P.current_user())
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.success(f"{uid} enctoken saved.")
                    st.rerun()
            elif removed:
                try:
                    delete_account(db, uid)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.success(f"{uid} removed.")
                    st.rerun()
