"""Group Management — create groups and tag live Kite positions into them.

Each group is a basket of positions (or slices of them) with its own rupee
stoploss / target. Groups start as drafts; deploying one arms it for monitoring.
Only *open* positions (non-zero quantity) can be added — a leg that later gets
squared off stays in its group with its P&L frozen.
"""
import streamlit as st

from zerodha_trades.services import groups as G
from zerodha_trades.services import positions as P
from zerodha_trades.views import _helpers as H


def render(db):
    st.title("📦 Zerodha Trades — Group Management")
    H.render_flash()

    live, fetched_at, error = H.live_positions(db)
    if error:
        st.error(error)
    live_map = P.as_map(live)
    open_positions = P.open_only(live)

    head, refresh = st.columns([5, 1])
    head.caption(
        f"{len(open_positions)} open position(s) · "
        f"{len(live) - len(open_positions)} closed · "
        f"last fetched {H.ist(fetched_at)} IST"
    )
    if refresh.button("🔄 Refresh", width='stretch'):
        H.clear_positions_cache()
        st.rerun()

    _create_form(db)
    st.divider()

    all_groups = G.list_groups(db)
    if not all_groups:
        st.info("No groups yet — create one above, then tag open positions into it.")
        return

    st.subheader("Groups")
    for group in all_groups:
        _group_panel(db, group, live_map, open_positions)


# ----- create ------------------------------------------------------------
def _create_form(db):
    with st.expander("➕ Create a group", expanded=False):
        with st.form("ztrade_create_group", clear_on_submit=True):
            c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
            name = c1.text_input("Group name", placeholder="e.g. Aug Iron Condor")
            stoploss = c2.number_input(
                "Stoploss (₹)", value=None, step=500.0, format="%.2f",
                help="Fires when group P&L falls to or below this. May be negative "
                     "(cut a loss) or positive (lock in a profit floor). Leave blank to disarm.",
            )
            target = c3.number_input(
                "Target (₹)", value=None, step=500.0, format="%.2f",
                help="Fires when group P&L rises to or above this. Leave blank to disarm.",
            )
            c4.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
            alert_enabled = c4.checkbox("Alerts enabled", value=True)

            if st.form_submit_button("Create group", type="primary"):
                _, err = G.create_group(db, name, stoploss, target, alert_enabled)
                H.flash('error', err) if err else H.flash(
                    'success', f"Created '{name.strip()}'.")
                st.rerun()


# ----- one group ---------------------------------------------------------
def _group_panel(db, group, live_map, open_positions):
    mark = G.mark_group(db, group, live_map)
    badge = H.STATUS_BADGE.get(group.status, group.status)
    header = (f"**{group.name}** — {mark['n_legs']} instrument(s) · "
              f"P&L {H.money(mark['pnl'])}")

    with st.expander(header, expanded=(group.status == G.DRAFT)):
        st.markdown(
            f"{badge} &nbsp; P&L {H.colored_money(mark['pnl'])} &nbsp;·&nbsp; "
            f"SL {H.money(group.stoploss)} &nbsp;·&nbsp; TGT {H.money(group.target)} "
            f"&nbsp;·&nbsp; {'🔔 alerts on' if group.alert_enabled else '🔕 alerts off'}"
        )
        if group.status == G.TRIGGERED and group.trigger_message:
            st.warning(f"{group.trigger_message} ({H.ist(group.triggered_at)} IST)")

        _levels_form(db, group)
        _legs_editor(db, group, mark, live_map)
        _add_positions(db, group, open_positions)
        _lifecycle_bar(db, group)


def _levels_form(db, group):
    with st.form(f"ztrade_levels_{group.id}"):
        c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
        name = c1.text_input("Group name", value=group.name, key=f"nm_{group.id}")
        stoploss = c2.number_input("Stoploss (₹)", value=group.stoploss, step=500.0,
                                   format="%.2f", key=f"sl_{group.id}")
        target = c3.number_input("Target (₹)", value=group.target, step=500.0,
                                 format="%.2f", key=f"tg_{group.id}")
        c4.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        alert_enabled = c4.checkbox("Alerts enabled", value=group.alert_enabled,
                                    key=f"al_{group.id}")
        if st.form_submit_button("Save settings"):
            _, err = G.update_group(db, group, name=name, stoploss=stoploss,
                                    target=target, alert_enabled=alert_enabled)
            H.flash('error', err) if err else H.flash('success', f"Saved '{name}'.")
            st.rerun()


def _legs_editor(db, group, mark, live_map):
    st.markdown("**Positions in this group**")
    if not mark['legs']:
        st.caption("None yet — add open positions below.")
        return

    rows = []
    for item in mark['legs']:
        leg = item['leg']
        rows.append({
            'id': leg.id,
            'Instrument': leg.tradingsymbol,
            'Product': leg.product,
            'Position Qty': item['position_quantity'],
            'Group Qty': leg.quantity,
            'Avg': item['average_price'],
            'LTP': item['last_price'],
            'P&L': item['pnl'],
            'State': item['state'],
            'Remove': False,
        })
    # data_editor round-trips a list of dicts as a list of dicts — no DataFrame
    # needed for tables this small.
    edited = st.data_editor(
        rows,
        key=f"ztrade_legs_{group.id}",
        hide_index=True,
        width='stretch',
        column_order=['Instrument', 'Product', 'Position Qty', 'Group Qty',
                      'Avg', 'LTP', 'P&L', 'State', 'Remove'],
        column_config={
            'Group Qty': st.column_config.NumberColumn(
                "Group Qty", step=1,
                help="Signed quantity this group owns. Must match the position's "
                     "direction and stay within its size."),
            'Position Qty': st.column_config.NumberColumn("Position Qty", disabled=True),
            'Avg': st.column_config.NumberColumn("Avg", format="%.2f", disabled=True),
            'LTP': st.column_config.NumberColumn("LTP", format="%.2f", disabled=True),
            'P&L': st.column_config.NumberColumn("P&L", format="%.2f", disabled=True),
            'State': st.column_config.TextColumn("State", disabled=True),
            'Instrument': st.column_config.TextColumn("Instrument", disabled=True),
            'Product': st.column_config.TextColumn("Product", disabled=True),
            'Remove': st.column_config.CheckboxColumn("Remove"),
        },
    )

    if st.button("Apply changes", key=f"ztrade_apply_{group.id}"):
        by_id = {leg.id: leg for leg in G.legs_of(db, group.id)}
        errors, removed, changed = [], 0, 0
        for row in edited:
            leg = by_id.get(int(row['id']))
            if leg is None:
                continue
            if bool(row['Remove']):
                G.remove_leg(db, leg)
                removed += 1
                continue
            new_qty = int(row['Group Qty'])
            if new_qty != leg.quantity:
                err = G.set_leg_quantity(db, leg, new_qty, live_map)
                if err:
                    errors.append(err)
                else:
                    changed += 1
        for err in errors:
            H.flash('error', err)
        if changed or removed:
            H.flash('success', f"'{group.name}': {changed} quantity change(s), "
                               f"{removed} removed.")
        elif not errors:
            H.flash('info', "Nothing to apply — no quantities changed.")
        st.rerun()


def _add_positions(db, group, open_positions):
    st.markdown("**Add open positions**")
    taken = {(leg.tradingsymbol, leg.product) for leg in G.legs_of(db, group.id)}
    available = [p for p in open_positions
                 if P.position_key(p['tradingsymbol'], p['product']) not in taken]
    if not available:
        st.caption("Every open position is already in this group."
                   if open_positions else "No open positions to add.")
        return

    # How much of each position other groups have already claimed — shown so
    # over-allocating the same leg across groups is visible, not silent.
    elsewhere = G.allocation_map(db, exclude_group_id=group.id)
    rows = [{
        'key': f"{p['tradingsymbol']}|{p['product']}",
        'Add': False,
        'Instrument': p['tradingsymbol'],
        'Product': p['product'],
        'Position Qty': p['quantity'],
        'Qty for group': p['quantity'],   # default: the whole position
        'In other groups': elsewhere.get(P.position_key(p['tradingsymbol'], p['product']), 0),
        'Avg': p['average_price'],
        'LTP': p['last_price'],
        'P&L': p['pnl'],
    } for p in available]

    edited = st.data_editor(
        rows,
        key=f"ztrade_add_{group.id}",
        hide_index=True,
        width='stretch',
        column_order=['Add', 'Instrument', 'Product', 'Position Qty', 'Qty for group',
                      'In other groups', 'Avg', 'LTP', 'P&L'],
        column_config={
            'Add': st.column_config.CheckboxColumn("Add"),
            'Qty for group': st.column_config.NumberColumn(
                "Qty for group", step=1,
                help="Defaults to the full position. Reduce it to tag only part."),
            'Position Qty': st.column_config.NumberColumn("Position Qty", disabled=True),
            'In other groups': st.column_config.NumberColumn("In other groups", disabled=True),
            'Instrument': st.column_config.TextColumn("Instrument", disabled=True),
            'Product': st.column_config.TextColumn("Product", disabled=True),
            'Avg': st.column_config.NumberColumn("Avg", format="%.2f", disabled=True),
            'LTP': st.column_config.NumberColumn("LTP", format="%.2f", disabled=True),
            'P&L': st.column_config.NumberColumn("P&L", format="%.2f", disabled=True),
        },
    )

    if st.button("Add selected to group", key=f"ztrade_addbtn_{group.id}", type="primary"):
        by_key = {f"{p['tradingsymbol']}|{p['product']}": p for p in available}
        picked = [r for r in edited if r.get('Add')]
        if not picked:
            st.warning("Tick the **Add** box on at least one position.")
            return
        errors, added, overflow = [], 0, []
        for row in picked:
            pos = by_key.get(row['key'])
            if pos is None:
                continue
            qty = int(row['Qty for group'])
            _, err = G.add_leg(db, group, pos, qty)
            if err:
                errors.append(err)
                continue
            added += 1
            other = elsewhere.get(P.position_key(pos['tradingsymbol'], pos['product']), 0)
            if abs(other + qty) > abs(pos['quantity']):
                overflow.append(f"{pos['tradingsymbol']}: {other + qty} tagged across "
                                f"groups vs a position of {pos['quantity']}")
        for err in errors:
            H.flash('error', err)
        if overflow:
            H.flash('warning', "Allocated across groups beyond the actual position — "
                    + "; ".join(overflow))
        if added:
            H.flash('success', f"Added {added} position(s) to '{group.name}'.")
        st.rerun()


def _lifecycle_bar(db, group):
    st.divider()
    c1, c2, c3, c4 = st.columns([1, 1, 1, 3])
    if group.status == G.DRAFT:
        if c1.button("🚀 Deploy", key=f"ztrade_dep_{group.id}", type="primary",
                     width='stretch'):
            ok, err = G.deploy(db, group)
            H.flash('success', f"'{group.name}' deployed — now monitored.") if ok \
                else H.flash('error', err)
            st.rerun()
    else:
        if c1.button("⏸ Undeploy", key=f"ztrade_und_{group.id}", width='stretch'):
            G.undeploy(db, group)
            H.flash('info', f"'{group.name}' returned to draft.")
            st.rerun()
        c4.caption(f"Deployed {H.ist(group.deployed_at)} IST")

    # Delete is gated on an explicit tick rather than a click-twice flag, which
    # would stay armed across unrelated interactions.
    confirm = c2.checkbox("confirm", key=f"ztrade_delok_{group.id}",
                          help="Tick to enable Delete.")
    if c3.button("🗑 Delete", key=f"ztrade_del_{group.id}", width='stretch',
                 disabled=not confirm):
        name = group.name
        G.delete_group(db, group)
        H.flash('success', f"Deleted '{name}' and its legs.")
        st.rerun()
