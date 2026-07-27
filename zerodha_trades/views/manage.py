"""Group Management — create groups and tag live Kite positions into them.

One tab per configured Zerodha account. A group belongs to exactly one account:
two logins are separate books, so a group never spans them. Everything inside a
tab — the position list, the groups, the pickers — is that account's alone.

Each group is a basket of positions (or slices of them) with its own rupee
stoploss / target. Groups start as drafts; deploying one arms it for monitoring.
Only *open* positions (non-zero quantity) can be added — a leg that later gets
squared off stays in its group with its P&L frozen.
"""
import streamlit as st

from zerodha_trades.services import groups as G
from zerodha_trades.services import positions as P
from zerodha_trades.views import _helpers as H


def _panel_key(group_id):
    """Session-state key holding a group panel's open/closed state.

    Giving the expander a key (with on_change) makes Streamlit persist the
    user's own toggle as widget state, so it survives reruns and label changes.
    Without it, the expander resets to the `expanded` argument whenever its
    label changes — and the label carries live P&L, so groups snapped shut on
    every button click.
    """
    return f"ztrade_grp_{group_id}"


def render(db):
    st.title("📦 Zerodha Trades — Group Management")
    H.inject_css()
    H.render_flash()

    books, fetched_at = H.live_positions_by_account(db)
    if not books:
        st.error("No Zerodha accounts configured — add one in **Broker Setup**.")
        return

    head, refresh = st.columns([5, 1])
    head.caption(f"{len(books)} account(s) · fetched in parallel "
                 f"(max {P.MAX_PARALLEL_ACCOUNTS} at a time) · "
                 f"last fetched {H.ist(fetched_at)} IST")
    if refresh.button("🔄 Refresh", width='stretch'):
        H.clear_positions_cache()
        st.rerun()

    # One lookup for every symbol on the page: the instruments master is global,
    # so accounts share it rather than each paying for its own fetch.
    all_groups = G.list_groups(db)
    lot_map = H.lot_sizes(db, [
        p['tradingsymbol'] for res in books.values() for p in res['positions']
    ] + [leg.tradingsymbol for g in all_groups for leg in G.legs_of(db, g.id)])

    user_ids = list(books)
    labels = [f"{uid}  ({len(G.list_groups(db, uid))})" for uid in user_ids]
    for tab, user_id in zip(st.tabs(labels), user_ids):
        with tab:
            _account_tab(db, user_id, books[user_id], lot_map)


def _account_tab(db, user_id, book, lot_map):
    """Everything for one account: its positions, its groups, its pickers."""
    if book['error']:
        st.error(f"**{user_id}** — could not fetch positions: {book['error']}")

    live = book['positions']
    live_map = P.as_map(live)
    open_positions = P.open_only(live)
    st.caption(f"{len(open_positions)} open · {len(live) - len(open_positions)} closed")

    unresolved = sorted({p['tradingsymbol'] for p in open_positions
                         if not lot_map.get(p['tradingsymbol'])})
    if unresolved:
        st.warning(
            "⚠️ Couldn't look up the lot size for "
            + ", ".join(f"**{s}**" for s in unresolved[:5])
            + (f" and {len(unresolved) - 5} more" if len(unresolved) > 5 else "")
            + ". Quantities for these can't be checked against their lot size, so "
              "adding, editing and deploying them is blocked. Press **🔄 Refresh**."
        )

    _open_positions_table(db, user_id, open_positions, lot_map)
    _create_form(db, user_id)
    st.divider()

    groups = G.list_groups(db, user_id)
    if not groups:
        st.info(f"No groups for **{user_id}** yet — create one above, then tag its "
                "open positions into it.")
        return
    for group in groups:
        _group_panel(db, group, live_map, open_positions, lot_map)


def _warn_imbalance(group):
    """Queue the lopsided-levels advisory, if the group's levels trip it."""
    note = G.levels_imbalance(group.stoploss, group.target)
    if note:
        H.flash('warning', f"⚠️ '{group.name}': {note}")


# ----- open positions ----------------------------------------------------
def _open_positions_table(db, user_id, open_positions, lot_map):
    """Read-only mirror of the Zerodha book.

    Every figure here is exactly what Kite reports — quantity, average, LTP and
    P&L are passed through untouched, with no group apportioning applied. Lot
    Size restates that same quantity in lots.
    """
    if not open_positions:
        return
    with st.expander(f"📋 {user_id} — open positions ({len(open_positions)})",
                     expanded=True):
        st.dataframe(
            [{
                'Instrument': p['tradingsymbol'],
                'Product': p['product'],
                'Qty': p['quantity'],
                'Lot Size': _lots(p['quantity'], lot_map.get(p['tradingsymbol'])),
                'Avg': p['average_price'],
                'LTP': p['last_price'],
                'P&L': p['pnl'],
            } for p in open_positions],
            hide_index=True,
            width='stretch',
            column_config={
                'Qty': st.column_config.NumberColumn(
                    "Qty", help="Position quantity as reported by Zerodha."),
                'Lot Size': st.column_config.NumberColumn(
                    "Lot Size",
                    help="Quantity in lots — Qty ÷ the contract's lot size "
                         "(e.g. -1300 ÷ 65 = -20 for NIFTY)."),
                'Avg': st.column_config.NumberColumn("Avg", format="%.2f"),
                'LTP': st.column_config.NumberColumn("LTP", format="%.2f"),
                'P&L': st.column_config.NumberColumn(
                    "P&L", format="%.2f", help="P&L as reported by Zerodha."),
            },
        )


def _differs(typed, shown):
    """True when a Closed P&L cell was actually edited.

    Both sides may be None (open leg, or an untouched blank), and floats
    round-trip through the grid, so compare to the paisa rather than exactly.
    """
    if typed is None and shown is None:
        return False
    if typed is None or shown is None:
        return True
    return abs(float(typed) - float(shown)) > 0.005


def _lots(quantity, lot_size):
    """Quantity restated in lots, or ``None`` when the lot size is unknown.

    Whole numbers come back as ints so a 20-lot position reads ``-20``, not
    ``-20.0``; anything that doesn't divide cleanly keeps two decimals.
    """
    if not lot_size:
        return None
    lots = quantity / lot_size
    return int(lots) if lots == int(lots) else round(lots, 2)


# ----- create ------------------------------------------------------------
def _create_form(db, user_id):
    with st.expander(f"➕ Create a group for {user_id}", expanded=False):
        # Keys carry the account: every tab renders at once, so a shared key
        # would make two accounts' forms the same widget.
        with st.form(f"ztrade_create_group_{user_id}", clear_on_submit=True):
            c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
            name = c1.text_input("Group name", placeholder="e.g. Aug Iron Condor",
                                 key=f"ztrade_newname_{user_id}")
            stoploss = c2.number_input(
                "Stoploss (₹)", value=None, step=500.0, format="%.2f",
                key=f"ztrade_newsl_{user_id}",
                help="Fires when group P&L falls to or below this. May be negative "
                     "(cut a loss) or positive (lock in a profit floor). Leave blank to disarm.",
            )
            target = c3.number_input(
                "Target (₹)", value=None, step=500.0, format="%.2f",
                key=f"ztrade_newtg_{user_id}",
                help="Fires when group P&L rises to or above this. Leave blank to disarm.",
            )
            c4.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
            alert_enabled = c4.checkbox("Alerts enabled", value=True,
                                        key=f"ztrade_newalert_{user_id}")

            if st.form_submit_button("Create group", type="primary"):
                group, err = G.create_group(db, name, user_id, stoploss, target,
                                            alert_enabled)
                if err:
                    H.flash('error', err)
                else:
                    # Open the new group once so its picker is on screen; from
                    # then on the expander's own state is the user's to control.
                    st.session_state[_panel_key(group.id)] = True
                    H.flash('success', f"Created '{group.name}' under {user_id} — "
                                       f"pick its positions below.")
                    _warn_imbalance(group)
                st.rerun()


# ----- one group ---------------------------------------------------------
def _group_panel(db, group, live_map, open_positions, lot_map):
    mark = G.mark_group(db, group, live_map)
    badge = H.STATUS_BADGE.get(group.status, group.status)
    header = (f"**{group.name}** · {group.user_id} — {mark['n_legs']} instrument(s) · "
              f"P&L {H.money(mark['pnl'])}")

    with st.expander(header, key=_panel_key(group.id), on_change="rerun"):
        st.markdown(
            f"{badge} &nbsp; P&L {H.colored_money(mark['pnl'])} &nbsp;·&nbsp; "
            f"SL {H.money(group.stoploss)} &nbsp;·&nbsp; TGT {H.money(group.target)} "
            f"&nbsp;·&nbsp; {'🔔 alerts on' if group.alert_enabled else '🔕 alerts off'}"
        )
        if group.status == G.TRIGGERED and group.trigger_message:
            st.warning(f"{group.trigger_message} ({H.ist(group.triggered_at)} IST)")

        _levels_form(db, group)
        _legs_editor(db, group, mark, live_map, lot_map)
        _add_positions(db, group, open_positions, lot_map)
        _lifecycle_bar(db, group, lot_map)


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
            if err:
                H.flash('error', err)
            else:
                H.flash('success', f"Saved '{name}'.")
                _warn_imbalance(group)
            st.rerun()


def _legs_editor(db, group, mark, live_map, lot_map):
    st.markdown("**Positions in this group** — double-click `Group Qty` to edit it, "
                "tick `Remove` to drop a leg, then press **Apply changes**.")
    st.caption("Quantities move in whole lots and can't exceed the position, so a "
               "single-lot leg has only one valid value — remove it rather than "
               "shrink it. Once a leg is closed its settled P&L can be corrected "
               "in **Closed P&L**; clear that cell to fall back to the automatic "
               "figure.")
    if not mark['legs']:
        st.caption("None yet — add open positions below.")
        return

    rows = []
    for item in mark['legs']:
        leg = item['leg']
        closed = item['state'] != 'open'
        rows.append({
            'id': leg.id,
            'state': item['state'],
            'Instrument': leg.tradingsymbol,
            'Group Qty': leg.quantity,
            'Avg': item['average_price'],
            'LTP': item['last_price'],
            'P&L': item['pnl'],
            # Editable only once the leg is settled; blank while it is open
            # so there is nothing to type into on a live position.
            'Closed P&L': item['pnl'] if closed else None,
            'State': item['state'] + (' (edited)' if item['overridden'] else ''),
            'Remove': False,
        })
    # data_editor round-trips a list of dicts as a list of dicts — no DataFrame
    # needed for tables this small. Avg and LTP are Zerodha's own numbers; only
    # P&L is ours, scaled from them to Group Qty.
    edited = st.data_editor(
        rows,
        key=f"ztrade_legs_{group.id}",
        hide_index=True,
        width='stretch',
        column_order=['Remove', 'Instrument', 'Group Qty', 'Avg', 'LTP',
                      'P&L', 'Closed P&L', 'State'],
        column_config={
            'Remove': st.column_config.CheckboxColumn("Remove", width="small"),
            'Group Qty': st.column_config.NumberColumn(
                "Group Qty", step=1,
                help="Signed quantity this group owns. Must match the position's "
                     "direction, stay within its size, and — for F&O — be a whole "
                     "number of lots."),
            'Avg': st.column_config.NumberColumn(
                "Avg", format="%.2f", disabled=True,
                help="Position average price from Zerodha."),
            'LTP': st.column_config.NumberColumn(
                "LTP", format="%.2f", disabled=True, help="Last price from Zerodha."),
            'P&L': st.column_config.NumberColumn(
                "P&L", format="%.2f", disabled=True,
                help="This group's share: Group Qty × (LTP − Avg)."),
            'Closed P&L': st.column_config.NumberColumn(
                "Closed P&L", format="%.2f", step=0.01,
                help="Settled P&L for a closed leg. Prefilled with this group's pro-rata share of the realised amount — correct it if the actual fill differed. Clear the cell to go back to the automatic figure. Blank while the leg is still open."),
            'State': st.column_config.TextColumn("State", disabled=True),
            'Instrument': st.column_config.TextColumn("Instrument", disabled=True),
        },
    )

    if st.button("Apply changes", key=f"ztrade_apply_{group.id}"):
        by_id = {leg.id: leg for leg in G.legs_of(db, group.id)}
        before = {r['id']: r['Closed P&L'] for r in rows}
        errors, removed, changed, repriced = [], 0, 0, 0
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
                err = G.set_leg_quantity(db, leg, new_qty, live_map,
                                         lot_map.get(leg.tradingsymbol))
                if err:
                    errors.append(err)
                else:
                    changed += 1
            # Closed-leg P&L override. Compare against what was rendered so
            # an untouched cell is never mistaken for an edit.
            shown = before.get(leg.id)
            typed = row.get('Closed P&L')
            if _differs(typed, shown):
                err = G.set_closed_pnl(db, leg, typed, row['state'])
                if err:
                    errors.append(err)
                else:
                    repriced += 1
        for err in errors:
            H.flash('error', err)
        if changed or removed or repriced:
            H.flash('success', f"'{group.name}': {changed} quantity change(s), "
                               f"{repriced} closed P&L edit(s), {removed} removed.")
        elif not errors:
            H.flash('info', "Nothing to apply — nothing changed.")
        st.rerun()


def _add_positions(db, group, open_positions, lot_map):
    st.markdown("**Add open positions**")
    taken = {(leg.tradingsymbol, leg.product) for leg in G.legs_of(db, group.id)}
    available = [p for p in open_positions
                 if P.position_key(p['tradingsymbol'], p['product']) not in taken]
    if not available:
        st.caption("Every open position is already in this group."
                   if open_positions else "No open positions to add.")
        return

    # Still tracked, just not shown: cross-group over-allocation is reported as a
    # warning when adding rather than as a column here.
    elsewhere = G.allocation_map(db, exclude_group_id=group.id, user_id=group.user_id)
    rows = [{
        'key': f"{p['tradingsymbol']}|{p['product']}",
        'Add': False,
        'Instrument': p['tradingsymbol'],
        'Product': p['product'],
        'Position Qty': p['quantity'],
        'Qty for group': p['quantity'],   # default: the whole position
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
                      'Avg', 'LTP', 'P&L'],
        column_config={
            'Add': st.column_config.CheckboxColumn("Add"),
            'Qty for group': st.column_config.NumberColumn(
                "Qty for group", step=1,
                help="Defaults to the full position. Reduce it to tag only part — "
                     "for F&O it must stay a whole number of lots."),
            'Position Qty': st.column_config.NumberColumn(
                "Position Qty", disabled=True, help="Quantity from Zerodha."),
            'Instrument': st.column_config.TextColumn("Instrument", disabled=True),
            'Product': st.column_config.TextColumn("Product", disabled=True),
            'Avg': st.column_config.NumberColumn("Avg", format="%.2f", disabled=True),
            'LTP': st.column_config.NumberColumn("LTP", format="%.2f", disabled=True),
            'P&L': st.column_config.NumberColumn(
                "P&L", format="%.2f", disabled=True, help="P&L from Zerodha."),
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
            _, err = G.add_leg(db, group, pos, qty, lot_map.get(pos['tradingsymbol']))
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


def _lifecycle_bar(db, group, lot_map):
    st.divider()
    c1, c2, c3, c4 = st.columns([1, 1, 1, 3])
    if group.status == G.DRAFT:
        if c1.button("🚀 Deploy", key=f"ztrade_dep_{group.id}", type="primary",
                     width='stretch'):
            ok, err = G.deploy(db, group, lot_map)
            if ok:
                H.flash('success', f"'{group.name}' deployed — now monitored.")
                _warn_imbalance(group)
            else:
                H.flash('error', err)
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
        st.session_state.pop(_panel_key(group.id), None)
        G.delete_group(db, group)
        H.flash('success', f"Deleted '{name}' and its legs.")
        st.rerun()
