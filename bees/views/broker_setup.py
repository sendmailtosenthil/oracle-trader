"""Broker Setup page: configure Zerodha / Kite credentials.

Several Zerodha logins can be held at once — one enctoken each. PC8006 is the
**master** account and is the one every automated job (downloader, momentum,
bot, position poller) uses; the extra accounts are stored purely so their
positions and tokens are on hand.
"""
import streamlit as st

from common.broker import (
    MASTER_USER_ID,
    delete_account,
    get_account,
    is_zerodha_token_valid,
    list_accounts,
    master_account,
    normalise_user_id,
    save_account,
)


def _status(account):
    """A one-glance validity badge for an account's stored token."""
    if not account or not account.enctoken:
        return "⚪ no token saved"
    if is_zerodha_token_valid(account.enctoken, account.user_id):
        return "✅ token valid"
    return "❌ token expired or invalid"


def render(db):
    st.title("Broker Setup & Integrations")
    st.write("Configure your API keys and tokens for broker integration.")

    st.subheader("Zerodha / Kite")

    master = master_account(db)
    others = [a for a in list_accounts(db) if a is not master]

    st.markdown(f"#### Master account — `{MASTER_USER_ID}`")
    st.caption(
        "Used by every automated job: downloader, momentum, the bot and the "
        "position poller. Its User ID is fixed."
    )
    st.write(_status(master))

    with st.form(key="zerodha_master_form"):
        st.text_input("Zerodha User ID", value=MASTER_USER_ID, disabled=True)
        m_enctoken = st.text_input(
            "Kite enctoken", value=(master.enctoken if master else ""), type="password"
        )
        if st.form_submit_button("Save master enctoken", type="primary"):
            try:
                save_account(db, MASTER_USER_ID, m_enctoken)
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
        st.info("No additional accounts yet — add one below.")

    for account in others:
        uid = normalise_user_id(account.user_id)
        with st.expander(f"{uid} — {_status(account)}"):
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
                    save_account(db, uid, enctoken)
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

    with st.form(key="zerodha_add_account_form", clear_on_submit=True):
        st.markdown("**Add an account**")
        new_user_id = st.text_input("Zerodha User ID", placeholder="e.g. AB1234")
        new_enctoken = st.text_input("Kite enctoken", type="password")

        if st.form_submit_button("Add account"):
            uid = normalise_user_id(new_user_id)
            if get_account(db, uid):
                st.error(f"{uid} is already configured — edit it above.")
            else:
                try:
                    save_account(db, uid, new_enctoken)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.success(f"{uid} added.")
                    st.rerun()
