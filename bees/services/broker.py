"""Broker (Zerodha / Kite) integration helpers."""
import requests
import streamlit as st


@st.cache_data(ttl=3600, show_spinner=False)
def is_zerodha_token_valid(enctoken):
    """Return True if the Kite enctoken can fetch the user profile. Cached for 1h.

    Call ``is_zerodha_token_valid.clear()`` after saving a new token to invalidate.
    """
    if not enctoken:
        return False
    try:
        headers = {"Authorization": f"enctoken {enctoken}"}
        res = requests.get("https://kite.zerodha.com/oms/user/profile/full", headers=headers, timeout=3)
        return res.status_code == 200
    except Exception:
        return False
