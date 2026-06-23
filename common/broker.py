"""Broker (Zerodha / Kite) integration helpers.

The actual Kite communication lives in :mod:`common.zerodha_client`.
This module exposes the Streamlit-cached token-validity check used across the
UI (by both the ``bees`` and ``downloader`` modules); it simply delegates to
``ZerodhaClient.validate()``.
"""
import streamlit as st

from common.zerodha_client import ZerodhaClient


@st.cache_data(ttl=3600, show_spinner=False)
def is_zerodha_token_valid(enctoken, user_id="PC8006"):
    """Return True if the Kite enctoken can fetch the user profile. Cached for 1h.

    Call ``is_zerodha_token_valid.clear()`` after saving a new token to invalidate.
    """
    if not enctoken:
        return False
    return ZerodhaClient(enctoken, user_id=user_id).validate()
