"""Per-page access control for the Streamlit app.

Every page in the app has a key (``"bees.dashboard"``, ``"ztrade.manage"`` …)
and every user carries a level for each key:

``none``
    The page is not in the sidebar and cannot be reached by URL.
``read``
    The page renders, but its editing controls are hidden or disabled.
``edit``
    Full access.

Some pages exist only to change things (Operations, Rebalance, Group
Management, Broker Setup) — a read-only view of them would be an empty form, so
they offer ``none``/``edit`` only.

Permissions live in ``users.permissions`` as a small JSON object; a user flagged
``is_admin`` bypasses the map and gets ``edit`` everywhere, including User
Management, which is never assignable to a non-admin.

The registry below is the single source of truth: ``app.py`` builds its
navigation from it and the User Management page builds its editor from it, so a
new page is added in one place.
"""
import json

NONE = "none"
READ = "read"
EDIT = "edit"

_RANK = {NONE: 0, READ: 1, EDIT: 2}

LEVEL_LABELS = {NONE: "No access", READ: "Read only", EDIT: "Edit"}


class Page:
    """One navigable page and the levels that may be granted on it."""

    def __init__(self, key, section, title, icon, url_path, readable=True,
                 admin_only=False, help=""):
        self.key = key
        self.section = section
        self.title = title
        self.icon = icon
        self.url_path = url_path
        self.readable = readable      # False => the page is an editor, none/edit only
        self.admin_only = admin_only  # never assignable; admins always see it
        self.help = help

    @property
    def levels(self):
        return (NONE, READ, EDIT) if self.readable else (NONE, EDIT)


PAGES = [
    Page("bees.dashboard", "🐝 Bees", "Dashboard", "📊", "dashboard",
         help="Live portfolio valuation, signals and charts."),
    Page("bees.operations", "🐝 Bees", "Operations (SIP / Batches)", "🔁", "operations",
         readable=False, help="Logs switches, SIPs, overrides and withdrawals."),
    Page("bees.ledger", "🐝 Bees", "Ledger & History", "📒", "ledger",
         help="Read shows holdings, cash flows and trades; edit allows inline changes."),

    Page("downloader.download", "📥 Downloader", "Options Download", "⬇️", "options-download",
         help="Read shows download history; edit can start a download."),

    Page("momentum.dashboard", "📈 Momentum", "Dashboard", "📊", "momentum",
         help="Holdings, ranking and performance."),
    Page("momentum.rebalance", "📈 Momentum", "Rebalance", "🔁", "momentum-rebalance",
         readable=False, help="Executes rebalances, refreshes prices and edits settings."),
    Page("momentum.ledger", "📈 Momentum", "Ledger & History", "📒", "momentum-ledger",
         help="Momentum trade history."),

    Page("ztrade.dashboard", "📦 Zerodha Trades", "Dashboard", "📊", "ztrade",
         help="Group P&L cards and position detail."),
    Page("ztrade.manage", "📦 Zerodha Trades", "Group Management", "🗂️", "ztrade-groups",
         readable=False, help="Creates groups, sets stoploss/target and tags positions."),

    Page("setup.broker", "⚙️ Setup", "Broker Setup", "🔑", "broker-setup",
         readable=False, help="Holds the Zerodha enctokens — grant sparingly."),
    Page("setup.users", "⚙️ Setup", "User Management", "👥", "users",
         readable=False, admin_only=True, help="Administrators only."),
]

BY_KEY = {p.key: p for p in PAGES}

# Section order as it should appear in the sidebar.
SECTIONS = list(dict.fromkeys(p.section for p in PAGES))


def pages_in(section):
    return [p for p in PAGES if p.section == section]


def assignable_pages():
    """Pages an administrator can grant to someone (everything but admin-only)."""
    return [p for p in PAGES if not p.admin_only]


# ---------------------------------------------------------------- the model

def empty():
    """A permission map granting nothing."""
    return {p.key: NONE for p in assignable_pages()}


def normalise(raw):
    """Coerce anything stored/submitted into a valid ``{key: level}`` map.

    Unknown keys are dropped, and a level the page doesn't offer falls back to
    ``none`` — never upwards. So if a page later loses its read-only mode,
    whoever held ``read`` on it loses access rather than being handed ``edit``.
    """
    perms = empty()
    for key, level in (raw or {}).items():
        page = BY_KEY.get(key)
        if page is None or page.admin_only:
            continue
        perms[key] = level if level in page.levels else NONE
    return perms


def loads(text):
    """Parse the JSON blob held in ``users.permissions``."""
    if not text:
        return empty()
    try:
        raw = json.loads(text)
    except (TypeError, ValueError):
        return empty()
    return normalise(raw if isinstance(raw, dict) else {})


def dumps(perms):
    return json.dumps(normalise(perms), sort_keys=True)


def for_user(user):
    """The effective permission map for a ``User`` row (admins get everything)."""
    if user is None:
        return empty()
    if user.is_admin:
        return {p.key: EDIT for p in PAGES}
    return loads(user.permissions)


def summarise(user):
    """One-line description of what a user can reach, for the admin list."""
    if user.is_admin:
        return "Administrator — full access"
    perms = for_user(user)
    granted = [f"{BY_KEY[k].section.split(' ', 1)[-1]} › {BY_KEY[k].title}"
               f"{' (read)' if v == READ else ''}"
               for k, v in perms.items() if v != NONE]
    return ", ".join(granted) if granted else "No pages granted"


# ------------------------------------------------- the current session's view
# Streamlit is imported lazily so the CLI and cron jobs can use the model above
# without dragging the whole app in.

_STATE_PERMS = "_permissions"
_STATE_ADMIN = "_is_admin"


def _state():
    import streamlit as st
    return st.session_state


def activate(user):
    """Publish a user's effective permissions for the rest of this run."""
    state = _state()
    state[_STATE_PERMS] = for_user(user)
    state[_STATE_ADMIN] = bool(user is not None and user.is_admin)


def clear():
    state = _state()
    state.pop(_STATE_PERMS, None)
    state.pop(_STATE_ADMIN, None)


def is_admin():
    return bool(_state().get(_STATE_ADMIN))


def level(key):
    return _state().get(_STATE_PERMS, {}).get(key, NONE)


def can_view(key):
    return _RANK[level(key)] >= _RANK[READ]


def can_edit(key):
    return level(key) == EDIT


def guard(key):
    """Stop rendering a page the current user may not see.

    Pages are already filtered out of the navigation; this is the backstop for
    someone typing a URL straight in.
    """
    import streamlit as st
    if can_view(key):
        return
    st.title("Not available")
    st.error("You don't have access to this page. Pick one from the sidebar.")
    st.stop()


def readonly_note(key, what="this page"):
    """Show the read-only banner and report whether editing is allowed."""
    import streamlit as st
    if can_edit(key):
        return True
    st.info(f"👁️ Read-only access — you can view {what}, but not change anything here.")
    return False
