"""Group Management — create groups and tag live Kite positions into them.

One tab per configured Zerodha account. A group belongs to exactly one account:
two logins are separate books, so a group never spans them. Everything inside a
tab — the position list, the groups, the pickers — is that account's alone.

Each group is a basket of positions (or slices of them) with its own rupee
stoploss / target. Groups start as drafts; deploying one arms it for monitoring.
A leg that gets squared off stays in its group with its settled P&L banked, and
keeps it if the same contract is opened again.
"""
import streamlit as st

from common import broker as B
from common import permissions as ACL   # `P` is taken by positions in this module
from zerodha_trades.services import groups as G
from zerodha_trades.services import positions as P
from zerodha_trades.views import _helpers as H

PAGE = "ztrade.manage"


# Display labels, positionally matched to G.ALL_CHANNELS.
CHANNEL_LABELS = ["Email", "Telegram"]


def _channels(labels):
    """Map the multiselect's labels back to channel keys."""
    return [G.ALL_CHANNELS[CHANNEL_LABELS.index(l)] for l in labels]


def _channel_badge(group):
    picked = G.channels_of(group)
    if not picked:
        return "🔕 alerts off"
    names = ", ".join(CHANNEL_LABELS[G.ALL_CHANNELS.index(c)] for c in picked)
    return f"🔔 {names}"


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
    ACL.guard(PAGE)
    st.title("📦 Zerodha Trades — Group Management")
    H.inject_css()
    H.render_flash()

    # You manage the books of the Zerodha logins you added. Someone else's
    # account is not shown here at all: its positions are their business, and a
    # group can only be built out of positions you can see. Scoping the fetch
    # itself means no broker round-trip is spent on an account you can't see.
    mine = set(B.my_user_ids(db))
    if not B.list_accounts(db):
        st.error("No Zerodha accounts configured — add one in **Setup › Zerodha "
                 "Accounts**.")
        return

    books, fetched_at = H.live_positions_by_account(db, only=mine)
    if not books:
        st.info(
            "None of the configured Zerodha accounts were added by you, so there "
            "are no books to manage here. Add your own on **Setup › Zerodha "
            "Accounts**, or ask an administrator to share a group with you — "
            "shared groups appear on the **Dashboard**."
        )
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
    all_groups = [g for g in G.list_groups(db) if g.user_id in books]
    lot_map = H.lot_sizes(db, [
        p['tradingsymbol'] for res in books.values() for p in res['positions']
    ] + [leg.tradingsymbol for g in all_groups for leg in G.legs_of(db, g.id)])

    # Fixed order and fixed labels. `books` is filled in completion order by
    # the parallel fetch, so ordering by it made the tabs shuffle between
    # runs; and a label carrying a group count changes the moment a group is
    # created, which Streamlit treats as a different tab set and resets the
    # selection. The key keeps the chosen tab across reruns.
    user_ids = [uid for uid, _ in P.credentials(db) if uid in books]
    user_ids += [uid for uid in books if uid not in user_ids]
    tabs = st.tabs(user_ids, key="ztrade_account_tabs", on_change="rerun")
    for tab, user_id in zip(tabs, user_ids):
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
    # Closed positions are shown and taggable: a group formed around legs
    # that have since been squared off still needs to carry their P&L.

    # Only open positions care: a closed one is a settled figure, so its lot size
    # is never checked and an expired contract that no longer resolves is not a
    # problem to report.
    unresolved = sorted({p['tradingsymbol'] for p in open_positions
                         if p['quantity'] and not lot_map.get(p['tradingsymbol'])})
    if unresolved:
        st.warning(
            "⚠️ Couldn't look up the lot size for "
            + ", ".join(f"**{s}**" for s in unresolved[:5])
            + (f" and {len(unresolved) - 5} more" if len(unresolved) > 5 else "")
            + ". Quantities for these can't be checked against their lot size, so "
              "adding, editing and deploying them is blocked. Press **🔄 Refresh**."
        )

    _open_positions_table(db, user_id, live, lot_map)
    _create_form(db, user_id)
    st.divider()

    # Bank any cycle that finished while the poller was off (overnight, a
    # holiday, a restart). The poller does this every cycle during market hours;
    # doing it here too means opening the page is enough to catch up.
    G.bank_settled(db, G.mark_all(db, {user_id: live_map},
                                  groups=G.list_groups(db, user_id)))

    # The account is yours, so every group on it is editable here — bar one an
    # administrator built on it, which stays theirs.
    groups = [g for g in G.list_groups(db, user_id) if G.can_edit_group(g)]
    if not groups:
        st.info(f"No groups for **{user_id}** yet — create one above, then tag its "
                "open positions into it.")
        return
    for group in groups:
        _group_panel(db, group, live_map, live, lot_map)


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
    n_open = sum(1 for p in open_positions if p["quantity"])
    with st.expander(f"📋 {user_id} — positions ({n_open} open, "
                     f"{len(open_positions) - n_open} closed)", expanded=True):
        st.dataframe(
            [{
                'Instrument': p['tradingsymbol'],
                'Product': p['product'],
                'Qty': p['quantity'],
                'Lot Size': _lots(p['quantity'], lot_map.get(p['tradingsymbol'])),
                'Traded Qty': p['basis_quantity'],
                'Avg': p['average_price'],
                'LTP': p['last_price'],
                'P&L': p['pnl'],
                'State': 'open' if p['quantity'] else 'closed',
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
                'Traded Qty': st.column_config.NumberColumn(
                    "Traded Qty",
                    help="Size the position had. Same as Qty while open; for a "
                         "squared-off row Zerodha reports Qty 0, and this is what "
                         "it held before that."),
                'State': st.column_config.TextColumn(
                    "State", help="Zerodha reports a squared-off leg with Qty 0."),
                'Avg': st.column_config.NumberColumn("Avg", format="%.2f"),
                'LTP': st.column_config.NumberColumn("LTP", format="%.2f"),
                'P&L': st.column_config.NumberColumn(
                    "P&L", format="%.2f", help="P&L as reported by Zerodha."),
            },
        )


def _differs(typed, shown):
    """True when a Settled P&L cell was actually edited.

    Either side may be None (a cleared cell), and floats round-trip through the
    grid, so compare to the paisa rather than exactly.
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
        st.caption("Both levels must be crossed, not just touched: the stoploss "
                   "fires once P&L falls *below* it, the target once P&L rises "
                   "*above* it. Either may be negative or "
                   "positive — a positive stoploss is a profit floor. Leave one "
                   "blank to disarm that side.")
        with st.form(f"ztrade_create_group_{user_id}", clear_on_submit=True):
            c1, c2, c3, c4 = st.columns([3, 2, 2, 3])
            name = c1.text_input("Group name", placeholder="e.g. Aug Iron Condor",
                                 key=f"ztrade_newname_{user_id}")
            stoploss = c2.number_input(
                "Stoploss (₹)", value=None, step=500.0, format="%.2f",
                key=f"ztrade_newsl_{user_id}",
            )
            target = c3.number_input(
                "Target (₹)", value=None, step=500.0, format="%.2f",
                key=f"ztrade_newtg_{user_id}",
            )
            channels = c4.multiselect(
                "Notify via", CHANNEL_LABELS, default=CHANNEL_LABELS,
                key=f"ztrade_newch_{user_id}",
                placeholder="No alerts")

            shared = st.checkbox(
                "Share with other users (view only)", value=False,
                key=f"ztrade_newshare_{user_id}",
                help="Off by default. When on, anyone who can open the Zerodha "
                     "Trades dashboard sees this group's P&L. Only you can edit "
                     "it either way.")

            if st.form_submit_button("Create group", type="primary"):
                group, err = G.create_group(db, name, user_id, stoploss, target,
                                            _channels(channels),
                                            owner=ACL.current_user(), shared=shared)
                if err:
                    H.flash('error', err)
                else:
                    # Deliberately not auto-opened: which panels are expanded is
                    # the user's business, not something a save should change.
                    H.flash('success', f"Created '{group.name}' under {user_id} — "
                                       f"open it below to pick its positions.")
                    _warn_imbalance(group)
                st.rerun()


# ----- one group ---------------------------------------------------------
def _group_panel(db, group, live_map, positions, lot_map):
    mark = G.mark_group(db, group, live_map)
    badge = H.STATUS_BADGE.get(group.status, group.status)
    # Label must not change between runs: Streamlit resets an expander to its
    # default whenever the label does, which is what made panels snap shut on
    # Refresh. Live P&L and the leg count live inside the panel instead.
    header = f"**{group.name}** · {group.user_id}"

    with st.expander(header, key=_panel_key(group.id), on_change="rerun"):
        st.markdown(
            f"{badge} &nbsp; P&L {H.colored_money(mark['pnl'])} &nbsp;·&nbsp; "
            f"{mark['n_legs']} instrument(s), {mark['open_legs']} open &nbsp;·&nbsp; "
            f"SL {H.money(group.stoploss)} &nbsp;·&nbsp; TGT {H.money(group.target)} "
            f"&nbsp;·&nbsp; {_channel_badge(group)}"
        )
        if group.status == G.TRIGGERED and group.trigger_message:
            st.warning(f"{group.trigger_message} ({H.ist(group.triggered_at)} IST)")

        _levels_form(db, group)
        _legs_editor(db, group, mark, live_map, lot_map)
        _add_positions(db, group, positions, lot_map)
        _lifecycle_bar(db, group, live_map, lot_map)


def _levels_form(db, group):
    with st.form(f"ztrade_levels_{group.id}"):
        c1, c2, c3, c4 = st.columns([3, 2, 2, 3])
        name = c1.text_input("Group name", value=group.name, key=f"nm_{group.id}")
        stoploss = c2.number_input("Stoploss (₹)", value=group.stoploss, step=500.0,
                                   format="%.2f", key=f"sl_{group.id}")
        target = c3.number_input("Target (₹)", value=group.target, step=500.0,
                                 format="%.2f", key=f"tg_{group.id}")
        channels = c4.multiselect(
            "Notify via", CHANNEL_LABELS,
            default=[CHANNEL_LABELS[i] for i, c in enumerate(G.ALL_CHANNELS)
                     if c in G.channels_of(group)],
            key=f"ch_{group.id}", placeholder="No alerts")
        shared = st.checkbox(
            "Share with other users (view only)", value=G.is_shared(group),
            key=f"sh_{group.id}",
            help="When on, anyone who can open the Zerodha Trades dashboard sees "
                 "this group's P&L. Editing stays with you.")
        if st.form_submit_button("Save settings"):
            _, err = G.update_group(db, group, name=name, stoploss=stoploss,
                                    target=target, channels=_channels(channels),
                                    shared=shared)
            if err:
                H.flash('error', err)
            else:
                H.flash('success', f"Saved '{name}'.")
                _warn_imbalance(group)
            st.rerun()


def _state_label(item, open_now):
    """What the State cell says, including why a settled edit won't take."""
    if open_now:
        # A re-opened leg carries banked history that can't be touched while the
        # new position runs; one that has never closed has nothing to explain.
        return "open — settled locked" if item['has_settled'] else "open"
    if item['overridden']:
        return "closed (edited)"
    cycles = item['cycles']
    return f"closed · {cycles} cycle(s)" if cycles > 1 else "closed"


def _legs_editor(db, group, mark, live_map, lot_map):
    st.markdown("**Positions in this group** — double-click `Group Qty` to edit it, "
                "tick `Remove` to drop a leg, then press **Apply changes**.")
    st.caption("Quantities move in whole lots and can't exceed the position, so a "
               "single-lot leg has only one valid value — remove it rather than "
               "shrink it. **Settled** is what closed cycles already made, kept for "
               "good — blank until a position has actually closed, and correctable "
               "by hand on a closed leg (clear the cell to go back to the automatic "
               "figure). **Open** is the live mark of whatever is held right now. "
               "Close a contract and open it again and the two simply add up, with "
               "the settled half locked while the new position runs.")
    if not mark['legs']:
        st.caption("None yet — add open positions below.")
        return

    rows = []
    for item in mark['legs']:
        leg = item['leg']
        open_now = item['state'] == G.OPEN
        # Settled is blank until something has actually settled — a leg that has
        # never been closed has no settled P&L, and 0.00 would claim it settled at
        # nothing. Same for LTP with no live row: 0.00 would read as a price of
        # zero. Blank cells still take a typed correction.
        rows.append({
            'id': leg.id,
            'state': item['state'],
            'Instrument': leg.tradingsymbol,
            'Group Qty': leg.quantity,
            'Avg': item['average_price'] or 0.0,
            'LTP': item['last_price'] if item['last_price'] else float('nan'),
            'Open P&L': item['open_pnl'],
            'Settled P&L': item['settled'] if item['has_settled'] else float('nan'),
            'P&L': item['pnl'],
            # Only mention the lock where there is a settled figure to lock; on a
            # leg that has never closed there is nothing to say.
            'State': _state_label(item, open_now),
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
                      'Open P&L', 'Settled P&L', 'P&L', 'State'],
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
            'Open P&L': st.column_config.NumberColumn(
                "Open", format="%.2f", disabled=True,
                help="Live mark of the position held right now: Group Qty × "
                     "(LTP − Avg). Zero once it is closed."),
            'Settled P&L': st.column_config.NumberColumn(
                "Settled", format="%.2f", step=0.01,
                help="What closed cycles already made, kept even after Zerodha "
                     "stops reporting the position. Prefilled with this group's "
                     "pro-rata share of the settled amount — correct it if the "
                     "actual fill differed, or clear the cell to go back to the "
                     "automatic figure. Blank means nothing has settled on this "
                     "leg yet — not that it settled at zero. Editable once the leg "
                     "is closed; a leg showing an open position ignores an edit "
                     "here, because that part of its P&L is marked live."),
            'P&L': st.column_config.NumberColumn(
                "Total P&L", format="%.2f", disabled=True,
                help="Settled + Open — a contract closed and re-opened carries "
                     "both."),
            'State': st.column_config.TextColumn("State", disabled=True),
            'Instrument': st.column_config.TextColumn("Instrument", disabled=True),
        },
    )

    if st.button("Apply changes", key=f"ztrade_apply_{group.id}"):
        by_id = {leg.id: leg for leg in G.legs_of(db, group.id)}
        before = {r['id']: r['Settled P&L'] for r in rows}
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
            # Settled-P&L correction. Compare against what was rendered so an
            # untouched cell is never mistaken for an edit.
            shown = before.get(leg.id)
            typed = row.get('Settled P&L')
            if _differs(typed, shown):
                err = G.set_settled_pnl(db, leg, typed, row['state'])
                if err:
                    errors.append(err)
                else:
                    repriced += 1
        for err in errors:
            H.flash('error', err)
        if changed or removed or repriced:
            H.flash('success', f"'{group.name}': {changed} quantity change(s), "
                               f"{repriced} settled P&L edit(s), {removed} removed.")
        elif not errors:
            H.flash('info', "Nothing to apply — nothing changed.")
        st.rerun()


def _add_positions(db, group, positions, lot_map):
    st.markdown("**Add positions**")
    st.caption("Closed positions can be tagged too — the group keeps their "
               "settled P&L.")
    taken = {(leg.tradingsymbol, leg.product) for leg in G.legs_of(db, group.id)}
    available = [p for p in positions
                 if P.position_key(p['tradingsymbol'], p['product']) not in taken
                 and p['basis_quantity']]
    if not available:
        st.caption("Every position is already in this group."
                   if positions else "No positions to add.")
        return

    # Still tracked, just not shown: cross-group over-allocation is reported as a
    # warning when adding rather than as a column here.
    elsewhere = G.allocation_map(db, exclude_group_id=group.id, user_id=group.user_id)
    rows = [{
        'key': f"{p['tradingsymbol']}|{p['product']}",
        'Add': False,
        'Instrument': p['tradingsymbol'],
        'Product': p['product'],
        # A squared-off row reports Qty 0, so offer the size it held — that is
        # what a group can take a share of, and its settled P&L follows.
        'Position Qty': p['basis_quantity'],
        'Qty for group': p['basis_quantity'],   # default: the whole position
        'Avg': p['average_price'],
        'LTP': p['last_price'],
        'P&L': p['pnl'],
        'State': 'open' if p['quantity'] else 'closed',
    } for p in available]

    edited = st.data_editor(
        rows,
        key=f"ztrade_add_{group.id}",
        hide_index=True,
        width='stretch',
        column_order=['Add', 'Instrument', 'Position Qty', 'Qty for group',
                      'Avg', 'LTP', 'P&L', 'State'],
        column_config={
            'Add': st.column_config.CheckboxColumn("Add"),
            'Qty for group': st.column_config.NumberColumn(
                "Qty for group", step=1,
                help="Defaults to the full position. Reduce it to tag only part — "
                     "for F&O it must stay a whole number of lots."),
            'Position Qty': st.column_config.NumberColumn(
                "Position Qty", disabled=True,
                help="Size available to tag — the live quantity, or what a "
                     "squared-off position held."),
            'State': st.column_config.TextColumn("State", disabled=True),
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


def _lifecycle_bar(db, group, live_map, lot_map):
    st.divider()
    c1, c2, c3, c4 = st.columns([1, 1, 1, 3])
    if group.status == G.DRAFT:
        if c1.button("🚀 Deploy", key=f"ztrade_dep_{group.id}", type="primary",
                     width='stretch'):
            ok, err = G.deploy(db, group, lot_map, live_map=live_map,
                               baseline=H.deploy_baseline(db, group, live_map))
            if ok:
                H.flash('success', f"'{group.name}' deployed — now monitored.")
                if not G.has_baseline(group):
                    H.flash('info',
                            f"Couldn't price {group.name}'s underlying just now, so "
                            f"its payoff chart has no deploy-time reference band. "
                            f"Undeploy and redeploy to take one.")
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
