"""Project Oracle — Streamlit entry point.

Thin orchestration layer: page config, authentication, sidebar navigation, and
dispatch to the per-page render functions in ``bees.views``. All business logic
lives in ``bees.services`` and the page modules.
"""
import streamlit as st

from bees.database import get_db, Strategy, BrokerConfig
from bees.auth import require_auth, logout
from bees.styles import inject_global_css
from bees.services.broker import is_zerodha_token_valid
from bees.views import dashboard, operations, ledger, broker_setup

st.set_page_config(page_title="Project Oracle", layout="wide")
inject_global_css()

# Force Authentication
require_auth()

st.sidebar.title(f"Welcome, {st.session_state['username']}")
if st.sidebar.button("Logout"):
    logout()

# Navigation grouped by module. Each module gets its own collapsible submenu;
# "Bees" is expanded by default so the landing page is Bees -> Dashboard.
with st.sidebar.expander("🐝 Bees", expanded=True):
    page = st.radio(
        "Bees",
        ["Dashboard", "Operations (SIP / Batches)", "Ledger & History", "Broker Setup"],
        label_visibility="collapsed",
        key="bees_nav",
    )

db = next(get_db())
strategies = db.query(Strategy).all()

# Global Broker Check
broker_config = db.query(BrokerConfig).filter(BrokerConfig.broker_name == 'ZERODHA').first()
if not broker_config or not is_zerodha_token_valid(broker_config.enctoken):
    st.warning("🚨 **Zerodha Token Expired or Missing!** Your `enctoken` is invalid. Please navigate to the **Broker Setup** tab to update it.")

if page == "Dashboard":
    dashboard.render(db, strategies)
elif page == "Operations (SIP / Batches)":
    operations.render(db, strategies)
elif page == "Ledger & History":
    ledger.render(db, strategies)
elif page == "Broker Setup":
    broker_setup.render(db)
