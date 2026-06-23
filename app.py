"""Project Oracle — Streamlit entry point.

Thin orchestration layer: page config, authentication, sidebar navigation, and
dispatch to per-module page renderers. Cross-module infrastructure lives in
``common`` (database, Zerodha client, notifications); feature logic lives in the
``bees`` and ``downloader`` modules.

Navigation uses Streamlit's native ``st.navigation`` so each module's pages
render as clickable links grouped under a section header. The landing page is
token-aware: Broker Setup when the Zerodha enctoken is missing/expired,
otherwise the Bees Dashboard.
"""
import streamlit as st

from common.database import get_db, Strategy, BrokerConfig
from common.broker import is_zerodha_token_valid
from bees.auth import require_auth, logout
from bees.styles import inject_global_css
from bees.views import dashboard, operations, ledger, broker_setup
from downloader.views import page as downloader_page
from downloader.views import analytics as downloader_analytics
from momentum.views import dashboard as momentum_dashboard
from momentum.views import rebalance as momentum_rebalance
from momentum.views import ledger as momentum_ledger

st.set_page_config(page_title="Project Oracle", layout="wide")
inject_global_css()

# Force Authentication
require_auth()

st.sidebar.title(f"Welcome, {st.session_state['username']}")
if st.sidebar.button("Logout"):
    logout()

db = next(get_db())
strategies = db.query(Strategy).all()

# Token check drives both the warning and the default landing page.
broker_config = db.query(BrokerConfig).filter(BrokerConfig.broker_name == 'ZERODHA').first()
token_ok = bool(
    broker_config
    and is_zerodha_token_valid(broker_config.enctoken, broker_config.user_id)
)
if not token_ok:
    st.warning("🚨 **Zerodha Token Expired or Missing!** Your `enctoken` is invalid. Update it in **Broker Setup**.")


# Page renderers close over this run's db/strategies.
def _dashboard():
    dashboard.render(db, strategies)


def _operations():
    operations.render(db, strategies)


def _ledger():
    ledger.render(db, strategies)


def _options_download():
    downloader_page.render(db)


def _analytics():
    downloader_analytics.render(db)


def _momentum_dashboard():
    momentum_dashboard.render(db)


def _momentum_rebalance():
    momentum_rebalance.render(db)


def _momentum_ledger():
    momentum_ledger.render(db)


def _broker_setup():
    broker_setup.render(db)


# Land on Dashboard when the token is valid, otherwise on Broker Setup.
nav = st.navigation({
    "🐝 Bees": [
        st.Page(_dashboard, title="Dashboard", icon="📊",
                url_path="dashboard", default=token_ok),
        st.Page(_operations, title="Operations (SIP / Batches)", icon="🔁",
                url_path="operations"),
        st.Page(_ledger, title="Ledger & History", icon="📒",
                url_path="ledger"),
    ],
    "📥 Downloader": [
        st.Page(_options_download, title="Options Download", icon="⬇️",
                url_path="options-download"),
        st.Page(_analytics, title="Analytics", icon="📈",
                url_path="analytics"),
    ],
    "📈 Momentum": [
        st.Page(_momentum_dashboard, title="Dashboard", icon="📊",
                url_path="momentum"),
        st.Page(_momentum_rebalance, title="Rebalance", icon="🔁",
                url_path="momentum-rebalance"),
        st.Page(_momentum_ledger, title="Ledger & History", icon="📒",
                url_path="momentum-ledger"),
    ],
    "⚙️ Setup": [
        st.Page(_broker_setup, title="Broker Setup", icon="🔑",
                url_path="broker-setup", default=not token_ok),
    ],
})
nav.run()
