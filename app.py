"""Project Oracle — Streamlit entry point.

Thin orchestration layer: page config, authentication, sidebar navigation, and
dispatch to per-module page renderers. Cross-module infrastructure lives in
``common`` (database, Zerodha client, notifications, permissions); feature logic
lives in the ``bees``, ``downloader``, ``momentum`` and ``zerodha_trades``
modules.

Navigation uses Streamlit's native ``st.navigation`` so each module's pages
render as clickable links grouped under a section header. Which links appear is
driven by the signed-in user's per-page permissions (see
:mod:`common.permissions`): pages they have no access to are never built, and
each page also guards itself in case someone types its URL directly. The
landing page is the first page they can actually reach — Broker Setup when the
Zerodha enctoken is missing and they are allowed to fix it.
"""
import streamlit as st

from common.database import get_db, Strategy, BrokerConfig
from common.broker import is_zerodha_token_valid
from common import permissions as P
from common import users as U
from bees.auth import require_auth, logout
from bees.styles import inject_global_css
from bees.views import dashboard, operations, ledger, broker_setup
from downloader.views import page as downloader_page
from momentum.views import dashboard as momentum_dashboard
from momentum.views import rebalance as momentum_rebalance
from momentum.views import ledger as momentum_ledger
from zerodha_trades.views import manage as ztrade_manage
from zerodha_trades.views import dashboard as ztrade_dashboard
from admin import users as admin_users

st.set_page_config(page_title="Project Oracle", layout="wide")
inject_global_css()

# Force Authentication
require_auth()

db = next(get_db())

# Permissions are read fresh on every run, so a change an administrator makes
# takes effect on the other user's next click rather than at their next login.
# A login whose user row has since been deleted is shown the door.
me = U.get_user(db, st.session_state["username"])
if me is None:
    st.error("Your account no longer exists.")
    logout()
P.activate(me)

st.sidebar.title(f"Welcome, {me.username}")
if me.is_admin:
    st.sidebar.caption("👑 Administrator")
if st.sidebar.button("Logout"):
    logout()

strategies = db.query(Strategy).all()

# Token check drives both the warning and the default landing page.
broker_config = db.query(BrokerConfig).filter(BrokerConfig.broker_name == 'ZERODHA').first()
token_ok = bool(
    broker_config
    and is_zerodha_token_valid(broker_config.enctoken, broker_config.user_id)
)
if not token_ok and P.can_view("setup.broker"):
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


def _momentum_dashboard():
    momentum_dashboard.render(db)


def _momentum_rebalance():
    momentum_rebalance.render(db)


def _momentum_ledger():
    momentum_ledger.render(db)


def _ztrade_dashboard():
    ztrade_dashboard.render(db)


def _ztrade_manage():
    ztrade_manage.render(db)


def _broker_setup():
    broker_setup.render(db)


def _user_management():
    admin_users.render(db)


RENDERERS = {
    "bees.dashboard": _dashboard,
    "bees.operations": _operations,
    "bees.ledger": _ledger,
    "downloader.download": _options_download,
    "momentum.dashboard": _momentum_dashboard,
    "momentum.rebalance": _momentum_rebalance,
    "momentum.ledger": _momentum_ledger,
    "ztrade.dashboard": _ztrade_dashboard,
    "ztrade.manage": _ztrade_manage,
    "setup.broker": _broker_setup,
    "setup.users": _user_management,
}

# Land on Broker Setup when the token needs fixing and this user may fix it,
# otherwise on the first page they can reach.
default_key = ("setup.broker" if not token_ok and P.can_view("setup.broker")
               else next((p.key for p in P.PAGES if P.can_view(p.key)), None))

nav_pages = {}
for section in P.SECTIONS:
    pages = [
        st.Page(RENDERERS[p.key], title=p.title, icon=p.icon, url_path=p.url_path,
                default=(p.key == default_key))
        for p in P.pages_in(section) if P.can_view(p.key)
    ]
    if pages:
        nav_pages[section] = pages

if not nav_pages:
    st.title("Project Oracle")
    st.error("Your account has no pages assigned. Ask an administrator for access.")
    st.stop()

nav = st.navigation(nav_pages)
nav.run()
