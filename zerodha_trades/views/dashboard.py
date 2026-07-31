"""Zerodha Trades dashboard — one card per group, three to a row.

Reads the snapshot the poller writes rather than calling Kite itself, so the
page costs a couple of small queries no matter how often it refreshes. The card
grid lives in a fragment that reruns on its own, leaving the rest of the page
(and the sidebar) alone.
"""
import datetime

import streamlit as st

from common import permissions as ACL   # `P` is taken by positions in this module
from zerodha_trades import poller as PL
from zerodha_trades.services import groups as G
from zerodha_trades.services import positions as P
from zerodha_trades.views import _helpers as H
from zerodha_trades.views import payoff_chart

PAGE = "ztrade.dashboard"

CARDS_PER_ROW = 3

# Id of the group whose breakdown dialog is open, if any.
OPEN_DIALOG = "_ztrade_open_dialog"

# The two ways to read a group's book, inside the dialog.
TABLE_VIEW, PAYOFF_VIEW = "📋 Table", "📈 Payoff"


def render(db):
    ACL.guard(PAGE)
    st.title("📦 Zerodha Trades — Dashboard")
    H.inject_css()
    H.render_flash()

    _poller_bar(db)
    _cards(db)
    # Rendered from the main body, not from the auto-refreshing card fragment: a
    # fragment rerun on a timer fights the dialog's lifecycle, leaving Close
    # unable to dismiss it.
    _maybe_dialog(db)


def _maybe_dialog(db):
    """Open the breakdown dialog for whichever card asked for it."""
    group_id = st.session_state.get(OPEN_DIALOG)
    if group_id is None:
        return
    group = G.get_group(db, group_id)
    # Gone (deleted in another tab), or never this user's to open — the id lives
    # in session state, so the visibility rule is re-checked here, not trusted.
    if group is None or not G.can_view_group(group):
        st.session_state.pop(OPEN_DIALOG, None)
        return
    # Only the group: the dialog's body marks it for itself, on its own clock.
    _positions_dialog(db, group)


def _poller_bar(db):
    """Poller health, interval control, and the off-hours test override."""
    settings = G.get_settings(db)
    age = P.snapshot_age(db)
    fresh = age is not None and (datetime.datetime.utcnow() - age).total_seconds() <= 120
    window_ok, reason = PL.should_poll(settings)

    # Read-only viewers get the health line; the poller's controls are edits.
    editable = ACL.can_edit(PAGE)
    cols = st.columns([3, 1.4, 1.4, 1.2]) if editable else st.columns(1)
    c1 = cols[0]
    icon = "🟢" if fresh else ("🟡" if window_ok else "⚪")
    c1.caption(f"{icon} Last poll {H.ist(settings.last_poll_at)} IST · "
               f"prices {H.ist(age)} IST · {settings.last_poll_status or 'no polls yet'}")

    if editable:
        c2, c3, c4 = cols[1], cols[2], cols[3]
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


@st.fragment(run_every=H.LIVE_SECONDS)
def _cards(db):
    """The card grid, rerunning on its own off the stored snapshot.

    Shares :data:`H.LIVE_SECONDS` with the dialog so the page has one refresh
    cadence to reason about rather than two competing timers.
    """
    # Your groups, plus any another user chose to share. Administrators see the
    # lot. A group nobody shared with you is not merely hidden from the grid —
    # its P&L never reaches this page.
    all_groups = G.visible_groups(db)
    if not all_groups:
        if G.list_groups(db):
            st.info("No groups are visible to you. Groups belong to whoever "
                    "created them; ask them to tick **Share with other users** "
                    "on one, or add your own Zerodha account and build your own.")
        else:
            st.info("No groups yet — create one under **Group Management**.")
        return

    # Every account's cards on one page; each group marks against its own book.
    marks = G.mark_all(db, P.snapshot_maps(db), groups=all_groups)

    total = sum(m['pnl'] for m in marks)
    accounts = {m['group'].user_id for m in marks}
    n_shared = sum(1 for m in marks if not G.can_edit_group(m['group']))
    shared_note = f" · {n_shared} shared with you" if n_shared else ""
    st.markdown(f"**{len(marks)} group(s)** across **{len(accounts)} account(s)** · "
                f"combined P&L {H.colored_money(total)}{shared_note}")

    for start in range(0, len(marks), CARDS_PER_ROW):
        row = marks[start:start + CARDS_PER_ROW]
        columns = st.columns(CARDS_PER_ROW)
        for column, mark in zip(columns, row):
            with column:
                _card(mark)


def _card(mark):
    group, pnl = mark['group'], mark['pnl']
    with st.container(border=True):
        if not G.can_edit_group(group):
            badge = f"  👁️ *shared by {G.owner_of(group) or 'another user'}*"
        elif G.is_shared(group):
            badge = "  🔗 *shared*"
        else:
            badge = ""
        st.markdown(f"**{group.name}**  `{group.user_id}`  "
                    f"{H.STATUS_BADGE.get(group.status, group.status)}{badge}")
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
def _positions_dialog(db, group):
    """Per-instrument breakdown behind a card's P&L, as a table or a payoff chart.

    Only the frame: the header, which does not move, and the Close button. The
    figures live in :func:`_live_body`, which re-marks them on its own clock.
    """
    st.markdown(f"**{group.name}**  `{group.user_id}`  "
                f"{H.STATUS_BADGE.get(group.status, group.status)}"
                f" &nbsp;·&nbsp; SL {H.money(group.stoploss)}"
                f" &nbsp;·&nbsp; Target {H.money(group.target)}")

    if group.status == G.TRIGGERED and group.trigger_message:
        st.error(f"{group.trigger_message}  \n_{H.ist(group.triggered_at)} IST_")

    if not G.legs_of(db, group.id):
        st.info("This group has no positions yet.")
    else:
        _live_body(db, group.id)

    # Outside the live fragment: a form that re-rendered every few seconds would
    # wipe half-typed numbers. Owners only — sharing is read-only.
    if G.can_edit_group(group):
        _levels_form(db, group)

    # Outside the fragment on purpose: Close needs a full rerun to tear the
    # dialog down, which a fragment-scoped rerun would not give it.
    if st.button("Close", key=f"ztrade_dlgclose_{group.id}"):
        st.session_state.pop(OPEN_DIALOG, None)
        st.rerun()


def _levels_form(db, group):
    """Move a group's stoploss / target from here, and re-arm it on save.

    A basket that has hit its level is the moment you most want to change one —
    take the target up because the trend has further to run, or pull the
    stoploss in to lock what is left. Doing that from the card you are already
    looking at saves a trip to Group Management, and saving re-arms in the same
    move: a triggered group that stayed triggered would go on ignoring the new
    level, which is not what anyone means by changing it.
    """
    triggered = group.status == G.TRIGGERED
    verb = "Save & re-arm" if group.status != G.DEPLOYED else "Save levels"

    with st.expander("🎯 Stoploss / target", expanded=triggered):
        if triggered:
            st.caption("This group has hit a level and stopped being monitored. "
                       "Set new ones and it starts again from here.")
        with st.form(f"ztrade_dlg_levels_{group.id}"):
            c1, c2 = st.columns(2)
            stoploss = c1.number_input(
                "Stoploss (₹)", value=group.stoploss, step=500.0, format="%.2f",
                key=f"ztrade_dlgsl_{group.id}",
                help="Fires when P&L falls below this. Leave blank to disarm "
                     "that side; a positive value is a profit floor.")
            target = c2.number_input(
                "Target (₹)", value=group.target, step=500.0, format="%.2f",
                key=f"ztrade_dlgtg_{group.id}",
                help="Fires when P&L rises above this. Leave blank to disarm "
                     "that side.")
            if st.form_submit_button(verb, type="primary", width='stretch'):
                _save_levels(db, group, stoploss, target)


def _save_levels(db, group, stoploss, target):
    """Persist new levels and, unless it is already armed, arm the group."""
    _, err = G.update_group(db, group, stoploss=stoploss, target=target)
    if err:
        st.error(err)
        return

    # Already deployed: it keeps running against the new levels, and its
    # deploy-time baseline stays the one it was armed with. Only a group that is
    # not currently monitored gets armed — which re-takes that baseline.
    live_map = P.snapshot_maps(db).get(group.user_id, {})

    # Where the P&L already sits against the level just set. Re-arming into a
    # level the group is already past is legitimate — but it fires again on the
    # next poll, so say so rather than let the alert look like a bug.
    breach, _ = G.evaluate(group, G.mark_group(db, group, live_map)['pnl'])

    if group.status == G.DEPLOYED:
        H.flash('success', f"Levels updated — '{group.name}' stays armed.")
    else:
        ok, deploy_err = G.deploy(
            db, group,
            H.lot_sizes(db, [leg.tradingsymbol for leg in G.legs_of(db, group.id)]),
            baseline=H.deploy_baseline(db, group, live_map),
            live_map=live_map,
        )
        if not ok:
            st.error(deploy_err)
            return
        H.flash('success', f"'{group.name}' re-armed — monitoring again from now.")

    if breach:
        H.flash('warning',
                f"⚠️ '{group.name}' is already past its "
                f"{'target' if breach == G.TARGET else 'stoploss'} — it will trigger "
                f"again on the next poll. Move the level further out to keep it "
                f"running.")

    note = G.levels_imbalance(group.stoploss, group.target)
    if note:
        H.flash('warning', f"⚠️ '{group.name}': {note}")
    # A full rerun repaints the dialog's header with the new levels and status;
    # the flash carries the outcome across it.
    st.rerun()


@st.fragment(run_every=H.LIVE_SECONDS)
def _live_body(db, group_id):
    """The table or the chart, re-marked every few seconds so the dialog ticks.

    Re-reads the group and the snapshot rather than closing over the mark the
    dialog opened with: a fragment rerun does not re-run the page, so nothing
    else would ever refresh it. Only this block reruns, which is why the header
    and Close button sit outside it.
    """
    group = G.get_group(db, group_id)
    # Deleted or unshared in another tab while the dialog sat open — the same
    # re-check the dialog does on open, because a fragment outlives that check.
    if group is None or not G.can_view_group(group):
        st.warning("This group is no longer visible to you. Close and reopen.")
        return
    mark = G.mark_group(db, group, P.snapshot_maps(db).get(group.user_id, {}))

    # The table is the whole book, open and closed alike; the chart can only
    # speak for what is still open, so the table stays the default.
    view = st.segmented_control(
        "View", [TABLE_VIEW, PAYOFF_VIEW], default=TABLE_VIEW,
        key=f"ztrade_view_{group_id}", label_visibility="collapsed")
    if view == PAYOFF_VIEW:
        payoff_chart.render(db, mark)
    else:
        _positions_table(mark)


def _positions_table(mark):
    """Every leg the group holds, open and closed."""
    group = mark['group']
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
    st.markdown(f"### Total {H.colored_money(mark['pnl'])}")
    st.caption(f"{mark['n_legs']} instrument(s) · {mark['open_legs']} open · "
               f"priced {H.ist(group.last_evaluated_at)} IST · "
               f"re-marked every {H.LIVE_SECONDS}s off the poller's snapshot.")


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
