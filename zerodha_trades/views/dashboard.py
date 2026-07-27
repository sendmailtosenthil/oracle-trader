"""Zerodha Trades dashboard — one card per group, three to a row.

Reads the snapshot the poller writes rather than calling Kite itself, so the
page costs a couple of small queries no matter how often it refreshes. The card
grid lives in a fragment that reruns on its own, leaving the rest of the page
(and the sidebar) alone.
"""
import datetime

import streamlit as st

from zerodha_trades import poller as PL
from zerodha_trades.services import groups as G
from zerodha_trades.services import positions as P
from zerodha_trades.views import _helpers as H

CARDS_PER_ROW = 3

# Id of the group whose breakdown dialog is open, if any.
OPEN_DIALOG = "_ztrade_open_dialog"


def render(db):
    st.title("📦 Zerodha Trades — Dashboard")
    H.inject_css()

    _poller_bar(db)
    _cards(db)
    # Rendered from the main body, not from the auto-refreshing fragment: a
    # fragment rerun every 10s fights the dialog's lifecycle, leaving Close
    # unable to dismiss it.
    _maybe_dialog(db)


def _maybe_dialog(db):
    """Open the breakdown dialog for whichever card asked for it."""
    group_id = st.session_state.get(OPEN_DIALOG)
    if group_id is None:
        return
    group = G.get_group(db, group_id)
    if group is None:                      # deleted from another tab
        st.session_state.pop(OPEN_DIALOG, None)
        return
    live_map = P.snapshot_maps(db).get(group.user_id, {})
    _positions_dialog(G.mark_group(db, group, live_map))


def _poller_bar(db):
    """Poller health, interval control, and the off-hours test override."""
    settings = G.get_settings(db)
    age = P.snapshot_age(db)
    fresh = age is not None and (datetime.datetime.utcnow() - age).total_seconds() <= 120
    window_ok, reason = PL.should_poll(settings)

    c1, c2, c3, c4 = st.columns([3, 1.4, 1.4, 1.2])
    icon = "🟢" if fresh else ("🟡" if window_ok else "⚪")
    c1.caption(f"{icon} Last poll {H.ist(settings.last_poll_at)} IST · "
               f"prices {H.ist(age)} IST · {settings.last_poll_status or 'no polls yet'}")

    seconds = c2.number_input("Poll every (sec)", min_value=1, max_value=3600,
                              value=int(settings.poll_seconds or 10), step=5,
                              key="ztrade_poll_seconds")
    enabled = c3.checkbox("Poller on", value=bool(settings.poller_enabled),
                          key="ztrade_poller_on")
    test_mode = c3.checkbox(
        "Test mode", value=bool(settings.test_mode), key="ztrade_test_mode",
        help="Poll outside 09:15-15:45 IST and on non-trading days. For testing "
             "only — leave off so the poller stays quiet when the market is shut.")
    if c4.button("Save", width='stretch'):
        settings.poll_seconds = int(seconds)
        settings.poller_enabled = bool(enabled)
        settings.test_mode = bool(test_mode)
        db.commit()
        st.rerun()

    if settings.test_mode:
        st.warning("🧪 **Test mode is on** — polling ignores market hours and "
                   "trading days. Turn it off when you're done testing.")
    elif not window_ok and settings.poller_enabled:
        st.info(f"😴 Poller idle — {reason}. It resumes automatically on the next "
                "trading day at 09:15 IST. Tick **Test mode** to poll right now.")
    elif window_ok and not fresh:
        st.info("No recent poll. The poller runs inside the **oracle-api** service — "
                "check `journalctl -u oracle-api -f` if this persists.")


@st.fragment(run_every=10)
def _cards(db):
    """The card grid, rerunning on its own every 10s off the stored snapshot."""
    all_groups = G.list_groups(db)
    if not all_groups:
        st.info("No groups yet — create one under **Group Management**.")
        return

    # Every account's cards on one page; each group marks against its own book.
    marks = G.mark_all(db, P.snapshot_maps(db), groups=all_groups)

    total = sum(m['pnl'] for m in marks)
    accounts = {m['group'].user_id for m in marks}
    st.markdown(f"**{len(marks)} group(s)** across **{len(accounts)} account(s)** · "
                f"combined P&L {H.colored_money(total)}")

    for start in range(0, len(marks), CARDS_PER_ROW):
        row = marks[start:start + CARDS_PER_ROW]
        columns = st.columns(CARDS_PER_ROW)
        for column, mark in zip(columns, row):
            with column:
                _card(mark)


def _card(mark):
    group, pnl = mark['group'], mark['pnl']
    with st.container(border=True):
        st.markdown(f"**{group.name}**  `{group.user_id}`  "
                    f"{H.STATUS_BADGE.get(group.status, group.status)}")
        st.markdown(f"### {H.colored_money(pnl)}")
        st.caption(f"{mark['n_legs']} instrument(s) · {mark['open_legs']} open")

        # Plain text rather than st.metric: at one-third page width the metric
        # font truncates a rupee figure to "-₹1…".
        st.markdown(
            f"<div style='display:flex;justify-content:space-between;font-size:13px'>"
            f"<span><span style='color:#888'>SL</span> "
            f"<b>{H.money(group.stoploss)}</b></span>"
            f"<span><span style='color:#888'>Target</span> "
            f"<b>{H.money(group.target)}</b></span></div>",
            unsafe_allow_html=True,
        )
        _gauge_bar(group, pnl)

        if group.status == G.TRIGGERED and group.trigger_message:
            st.error(f"{group.trigger_message}\n\n_{H.ist(group.triggered_at)} IST_")
        elif group.status == G.DRAFT:
            st.caption("⚪ Not deployed — no monitoring.")
        elif not group.alert_enabled:
            st.caption("🔕 Alerts off — monitored but silent.")
        else:
            st.caption(f"🔔 Alerts on · marked {H.ist(group.last_evaluated_at)} IST")

        if st.button(f"View {mark['n_legs']} position(s)", key=f"ztrade_open_{group.id}",
                     width='stretch'):
            st.session_state[OPEN_DIALOG] = group.id
            st.rerun()   # full rerun: the dialog is rendered by the main body


def _forget_dialog():
    """Clear the open-dialog flag when the user dismisses via the X or Esc.

    Without this the flag survives the dismissal and the next full rerun — the
    poller-bar Save, say — would pop the dialog straight back open.
    """
    st.session_state.pop(OPEN_DIALOG, None)


@st.dialog("Group positions", width="large", on_dismiss=_forget_dialog)
def _positions_dialog(mark):
    """Per-instrument breakdown behind a card's P&L."""
    group, pnl = mark['group'], mark['pnl']
    st.markdown(f"**{group.name}**  `{group.user_id}`  "
                f"{H.STATUS_BADGE.get(group.status, group.status)}"
                f" &nbsp;·&nbsp; SL {H.money(group.stoploss)}"
                f" &nbsp;·&nbsp; Target {H.money(group.target)}")

    if not mark['legs']:
        st.info("This group has no positions yet.")
    else:
        st.dataframe(
            [{
                'Instrument': item['leg'].tradingsymbol,
                'Group Qty': item['leg'].quantity,
                'Avg': item['average_price'],
                'LTP': item['last_price'],
                'P&L': item['pnl'],
                'State': item['state'],
            } for item in mark['legs']],
            hide_index=True,
            width='stretch',
            column_config={
                'Avg': st.column_config.NumberColumn("Avg", format="%.2f"),
                'LTP': st.column_config.NumberColumn("LTP", format="%.2f"),
                'P&L': st.column_config.NumberColumn(
                    "P&L", format="%.2f",
                    help="Group Qty × (LTP − Avg) — this group's share of the leg."),
            },
        )
        st.markdown(f"### Total {H.colored_money(pnl)}")
        # Streamlit will not re-render a dialog that is already open, so this
        # is the book as of the moment it was opened even though the cards
        # behind it keep ticking. Timestamp it rather than imply it is live.
        st.caption(f"{mark['n_legs']} instrument(s) · {mark['open_legs']} open · "
                   f"priced {H.ist(group.last_evaluated_at)} IST — reopen for a "
                   f"newer mark.")

    if st.button("Close", key=f"ztrade_dlgclose_{group.id}"):
        st.session_state.pop(OPEN_DIALOG, None)
        st.rerun()


# Only one side is ever in play, so only one bar is ever drawn: red running
# toward the stoploss while the group is down, green toward the target while it
# is up. st.progress can't be coloured, hence the hand-rolled bar.
_RED = "#c0392b"
_GREEN = "#0a7d33"
_TRACK = "rgba(128,128,128,0.22)"   # readable on both light and dark themes


def _gauge(group, pnl):
    """``(fraction, colour, caption)`` for the level currently in play.

    The fraction is how far P&L has travelled toward that level, not its
    position between the two — a group at -₹5,000 against a -₹15,000 stoploss
    reads one third of the way to being stopped out.
    """
    if pnl < 0:
        level, colour, name = group.stoploss, _RED, "stoploss"
    else:
        level, colour, name = group.target, _GREEN, "target"

    if not level:
        return 0.0, colour, f"No {name} set"
    # Same-signed level and P&L give a positive ratio; an opposite-signed one
    # (a profit floor with the group down, say) clamps to empty rather than
    # drawing a bar that means nothing.
    fraction = min(1.0, max(0.0, pnl / level))
    return fraction, colour, f"{fraction * 100:.0f}% of the {H.money(level)} {name}"


def _gauge_bar(group, pnl):
    fraction, colour, caption = _gauge(group, pnl)
    st.markdown(
        f"<div style='background:{_TRACK};border-radius:4px;height:8px;"
        f"overflow:hidden;margin:4px 0 2px'>"
        f"<div style='width:{fraction * 100:.1f}%;height:100%;background:{colour};"
        f"border-radius:4px'></div></div>"
        f"<div style='font-size:12px;color:#888;margin-bottom:4px'>{caption}</div>",
        unsafe_allow_html=True,
    )
