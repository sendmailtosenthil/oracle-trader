"""Project Oracle — Streamlit entry point.

Thin orchestration layer: page config, authentication, sidebar navigation, and
dispatch to per-module page renderers. Cross-module infrastructure lives in
``common`` (database, Zerodha client, notifications); feature logic lives in the
``bees`` and ``downloader`` modules.
"""
import streamlit as st

from common.database import get_db, Strategy, BrokerConfig
from common.broker import is_zerodha_token_valid
from bees.auth import require_auth, logout
from bees.styles import inject_global_css
from bees.views import dashboard, operations, ledger, broker_setup
from downloader.views import page as downloader_page

st.set_page_config(page_title="Project Oracle", layout="wide")
inject_global_css()

# Force Authentication
require_auth()

st.sidebar.title(f"Welcome, {st.session_state['username']}")
if st.sidebar.button("Logout"):
    logout()

# Navigation grouped by module — each module gets its own collapsible submenu.
# A single ``active_page`` in session state is the source of truth; each radio
# updates it on change so the groups behave as one mutually-exclusive menu.
BEES_PAGES = ["Dashboard", "Operations (SIP / Batches)", "Ledger & History"]
DOWNLOADER_PAGES = ["Download"]
SETUP_PAGES = ["Broker Setup"]

if "active_page" not in st.session_state:
    st.session_state["active_page"] = "Dashboard"


def _select(nav_key):
    st.session_state["active_page"] = st.session_state[nav_key]


with st.sidebar.expander("🐝 Bees", expanded=True):
    st.radio("Bees", BEES_PAGES, label_visibility="collapsed",
             key="bees_nav", on_change=_select, args=("bees_nav",))

with st.sidebar.expander("📥 Downloader", expanded=False):
    st.radio("Downloader", DOWNLOADER_PAGES, label_visibility="collapsed",
             key="downloader_nav", on_change=_select, args=("downloader_nav",))

with st.sidebar.expander("⚙️ Setup", expanded=False):
    st.radio("Setup", SETUP_PAGES, label_visibility="collapsed",
             key="setup_nav", on_change=_select, args=("setup_nav",))

page = st.session_state["active_page"]

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
elif page == "Download":
    downloader_page.render(db)
elif page == "Broker Setup":
    broker_setup.render(db)
