"""Group definition, P&L marking, and trigger evaluation.

A group's P&L is the sum of its legs, each marked against the *position's
average price* and pro-rated to the quantity the group owns:

    leg_pnl = group_qty * (last_price - average_price)

so a group holding a whole position reports exactly the P&L Kite shows, and a
group holding half of it reports half. Once the broker squares the position off
the leg's share of the settled amount is banked, and stays banked after Kite
stops reporting the row — so a leg's P&L is really two numbers that add up, its
banked history plus the live mark of whatever it holds now. Close a contract and
open the same one again and both count.

Triggers are absolute rupee levels on that P&L: ``target`` is the upper bound
and ``stoploss`` the lower one. Both may be positive or negative, so a positive
stoploss works as a profit floor.
"""
import datetime

from common.database import TradeGroup, TradeGroupLeg, TradeGroupSetting

DRAFT = 'draft'
DEPLOYED = 'deployed'
TRIGGERED = 'triggered'

TARGET = 'TARGET'
STOPLOSS = 'STOPLOSS'


# ----- settings ----------------------------------------------------------
def get_settings(db):
    """The single settings row, created with defaults on first access."""
    row = db.query(TradeGroupSetting).first()
    if row is None:
        row = TradeGroupSetting()
        db.add(row)
        db.commit()
    return row


# ----- group CRUD --------------------------------------------------------
def list_groups(db, user_id=None):
    """Groups, optionally narrowed to one account."""
    query = db.query(TradeGroup)
    if user_id is not None:
        query = query.filter(TradeGroup.user_id == user_id)
    return query.order_by(TradeGroup.created_at.asc()).all()


def get_group(db, group_id):
    return db.query(TradeGroup).filter(TradeGroup.id == group_id).first()


# --- Who may see and change a group -----------------------------------------
# A group has two owners in different senses: `user_id` is the Kite login whose
# positions it holds, and `owner` is the app user who created it. Editing
# follows `owner`; viewing additionally allows anything flagged `shared`.
#
# These are UI rules only. The poller evaluates and alerts on every deployed
# group regardless — it runs with no signed-in user, and a stoploss must fire
# whoever happens to be looking.

def owner_of(group):
    return (getattr(group, "owner", "") or "").strip()


def is_shared(group):
    return bool(getattr(group, "shared", False))


def can_edit_group(group):
    """Only the creator (or an administrator) may change a group."""
    from common import permissions as P
    return P.owns(owner_of(group))


def can_view_group(group):
    """Creator, administrator, or anyone at all once the group is shared."""
    return can_edit_group(group) or is_shared(group)


def visible_groups(db, user_id=None):
    """Groups the signed-in user may see: their own, plus shared ones."""
    return [g for g in list_groups(db, user_id) if can_view_group(g)]


def editable_groups(db, user_id=None):
    return [g for g in list_groups(db, user_id) if can_edit_group(g)]


def legs_of(db, group_id):
    return (
        db.query(TradeGroupLeg)
        .filter(TradeGroupLeg.group_id == group_id)
        .order_by(TradeGroupLeg.tradingsymbol)
        .all()
    )


def create_group(db, name, user_id, stoploss=None, target=None, channels=None,
                 owner=None, shared=False):
    """Create a draft group on Zerodha account ``user_id``. Returns ``(group, error)``.

    ``channels`` is the notification channels to use (see
    :mod:`zerodha_trades.services.alerts`); ``None`` means all of them. An
    empty list is how a group is created silent.

    ``owner`` is the *app* user creating it — distinct from ``user_id``, which
    is the Kite login the positions belong to. ``shared`` opens the group up for
    other users to view; it never lets them edit.
    """
    name = (name or '').strip()
    if not name:
        return None, "Group name is required."
    if not user_id:
        return None, "A group must belong to a Zerodha account."
    clash = db.query(TradeGroup).filter(TradeGroup.name == name).first()
    if clash:
        # Names are unique across accounts, so say which one already has it
        # rather than leave the user guessing at an invisible collision.
        return None, (f"A group named '{name}' already exists"
                      + (f" under {clash.user_id}." if clash.user_id != user_id else "."))
    err = validate_levels(stoploss, target)
    if err:
        return None, err
    group = TradeGroup(name=name, user_id=user_id, stoploss=stoploss,
                       target=target, status=DRAFT, owner=owner,
                       shared=bool(shared))
    apply_channels(group, ALL_CHANNELS if channels is None else channels)
    db.add(group)
    db.commit()
    return group, None


def validate_levels(stoploss, target):
    """Stoploss must sit below target when both are armed."""
    if stoploss is not None and target is not None and stoploss >= target:
        return (f"Stoploss (₹{stoploss:,.2f}) must be below target (₹{target:,.2f}) — "
                "otherwise both trigger at once.")
    return None


# How far apart the risk and reward legs may sit before it looks like a typo.
LEVEL_BALANCE_TOLERANCE = 0.05


def levels_imbalance(stoploss, target, tolerance=LEVEL_BALANCE_TOLERANCE):
    """Advisory message when risk and reward are lopsided, else ``None``.

    Compares the *magnitudes* of the two levels: a stoploss of -1,50,000 against
    a 25,000 target is far more often a mistyped zero than an intended 6:1 risk.
    Purely a warning — the group still saves.
    """
    if stoploss is None or target is None:
        return None
    risk, reward = abs(stoploss), abs(target)
    larger = max(risk, reward)
    if larger == 0:
        return None
    gap = abs(risk - reward) / larger
    if gap <= tolerance:
        return None

    head = (f"Stoploss ₹{risk:,.2f} and target ₹{reward:,.2f} differ by "
            f"{gap * 100:.0f}%, over the {tolerance * 100:.0f}% tolerance")
    smaller = min(risk, reward)
    if not smaller:
        return f"{head} — one side is zero. Check for a mistyped value."
    side = "Risking" if risk > reward else "Targeting"
    return (f"{head} — {side.lower()} {larger / smaller:.1f}× the other side. "
            "Check for a mistyped zero.")


def update_group(db, group, name=None, stoploss=..., target=..., channels=None,
                 shared=None):
    """Patch a group's editable fields. Returns ``(group, error)``.

    ``stoploss`` / ``target`` use an ``...`` sentinel so ``None`` can be passed
    explicitly to disarm that side. ``shared`` is left alone when ``None``.
    """
    new_sl = group.stoploss if stoploss is ... else stoploss
    new_tg = group.target if target is ... else target
    err = validate_levels(new_sl, new_tg)
    if err:
        return group, err
    if name is not None:
        name = name.strip()
        if not name:
            return group, "Group name is required."
        clash = (
            db.query(TradeGroup)
            .filter(TradeGroup.name == name, TradeGroup.id != group.id)
            .first()
        )
        if clash:
            return group, (f"A group named '{name}' already exists"
                           + (f" under {clash.user_id}."
                              if clash.user_id != group.user_id else "."))
        group.name = name
    group.stoploss = new_sl
    group.target = new_tg
    if channels is not None:
        apply_channels(group, channels)
    if shared is not None:
        group.shared = bool(shared)
    db.commit()
    return group, None


# Notification channels live as one column each, with alert_enabled kept in
# step so the poller keeps a single thing to check.
EMAIL = 'email'
TELEGRAM = 'telegram'
ALL_CHANNELS = (EMAIL, TELEGRAM)


def apply_channels(group, channels):
    """Set a group's notification channels. No channels means alerts off."""
    picked = set(channels or ())
    group.notify_email = EMAIL in picked
    group.notify_telegram = TELEGRAM in picked
    group.alert_enabled = bool(picked)


def channels_of(group):
    """The channels a group notifies on, as a list."""
    picked = []
    if group.notify_email:
        picked.append(EMAIL)
    if group.notify_telegram:
        picked.append(TELEGRAM)
    return picked


def delete_group(db, group):
    db.query(TradeGroupLeg).filter(TradeGroupLeg.group_id == group.id).delete()
    db.delete(group)
    db.commit()


# ----- legs --------------------------------------------------------------
def add_leg(db, group, position, quantity=None, lot_size=None):
    """Tag a position (or a slice of it) into a group. Returns ``(leg, error)``.

    ``quantity`` defaults to the position's full quantity. It must be non-zero,
    point the same way as the position, not exceed it in magnitude, and — for
    derivatives — be a whole number of lots.
    """
    # A closed position is still taggable: it keeps its settled P&L in the
    # group. Validate against the size it had rather than today's zero.
    pos_qty = int(position.get('basis_quantity') or position['quantity'])
    if pos_qty == 0:
        return None, (f"{position['tradingsymbol']}: can't tell what size this "
                      f"position was — nothing to add.")
    qty = pos_qty if quantity is None else int(quantity)
    err = validate_leg_quantity(qty, pos_qty, position['tradingsymbol'], lot_size)
    if err:
        return None, err

    existing = (
        db.query(TradeGroupLeg)
        .filter(
            TradeGroupLeg.group_id == group.id,
            TradeGroupLeg.tradingsymbol == position['tradingsymbol'],
            TradeGroupLeg.product == position['product'],
        )
        .first()
    )
    if existing:
        return None, (f"{position['tradingsymbol']} ({position['product']}) is already "
                      f"in '{group.name}' — edit its quantity instead.")

    leg = TradeGroupLeg(
        group_id=group.id,
        tradingsymbol=position['tradingsymbol'],
        exchange=position['exchange'],
        product=position['product'],
        instrument_token=position['instrument_token'],
        quantity=qty,
        source_quantity=pos_qty,
        avg_price=position['average_price'],
    )
    db.add(leg)
    db.commit()
    return leg, None


def validate_leg_quantity(qty, pos_qty, symbol, lot_size=None):
    """Leg quantity must be non-zero, same-signed, and within the position.

    It must also be a whole number of lots. An unresolved ``lot_size`` is a
    *failure*, not a free pass: without it the whole-lot rule cannot be checked,
    and letting the quantity through unchecked is exactly how a bad one reaches
    a deployed group.
    """
    if qty == 0:
        return f"{symbol}: quantity cannot be 0."
    if (qty > 0) != (pos_qty > 0):
        side = "long" if pos_qty > 0 else "short"
        return (f"{symbol}: the position is {side} ({pos_qty}) — group quantity "
                f"must have the same sign.")
    if abs(qty) > abs(pos_qty):
        return (f"{symbol}: group quantity {qty} exceeds the position quantity "
                f"{pos_qty}.")
    if not lot_size:
        return lot_size_unavailable(symbol)
    # A one-lot position has exactly one legal quantity, so say that outright
    # rather than let the caller guess from a lot-multiple complaint.
    if lot_size and lot_size > 1 and abs(pos_qty) == lot_size and abs(qty) != lot_size:
        return (f"{symbol}: the position is a single lot ({pos_qty}), so {pos_qty} is "
                f"the only valid group quantity. Tick Remove to drop the leg instead.")
    return validate_lot_multiple(qty, symbol, lot_size)


def lot_size_unavailable(symbol):
    return (f"{symbol}: couldn't look up the lot size, so the quantity can't be "
            f"checked against it. Press Refresh and try again.")


def validate_lot_multiple(qty, symbol, lot_size):
    """Reject a quantity that isn't a whole number of lots.

    A lot size of 1 (cash equity) makes every quantity a whole lot. A missing
    lot size means we could not verify, which is treated as a failure.
    """
    if not lot_size:
        return lot_size_unavailable(symbol)
    if lot_size <= 1 or abs(qty) % lot_size == 0:
        return None
    sign = -1 if qty < 0 else 1
    lots_below = abs(qty) // lot_size
    nearest = [sign * lots_below * lot_size, sign * (lots_below + 1) * lot_size]
    options = " or ".join(str(n) for n in nearest if n)
    return (f"{symbol}: quantity {qty} is not a whole number of lots — the lot "
            f"size is {lot_size} ({abs(qty) / lot_size:.2f} lots). Use {options}.")


def set_leg_quantity(db, leg, quantity, live_map=None, lot_size=None):
    """Change a leg's quantity, validated against the live position if known."""
    qty = int(quantity)
    live = (live_map or {}).get((leg.tradingsymbol, leg.product))
    basis = int(live['quantity']) if live and live['quantity'] else leg.source_quantity
    err = validate_leg_quantity(qty, basis, leg.tradingsymbol, lot_size)
    if err:
        return err
    leg.quantity = qty
    db.commit()
    return None


def remove_leg(db, leg):
    db.delete(leg)
    db.commit()


def allocation_map(db, exclude_group_id=None, user_id=None):
    """Total quantity already tagged per position, across an account's groups.

    Used to warn when the same broker position is spread over several groups by
    more than it actually holds. Scoped by account: two logins holding the same
    contract are unrelated books and must not pool their allocations.
    """
    q = db.query(TradeGroupLeg)
    if exclude_group_id is not None:
        q = q.filter(TradeGroupLeg.group_id != exclude_group_id)
    if user_id is not None:
        q = q.join(TradeGroup, TradeGroup.id == TradeGroupLeg.group_id).filter(
            TradeGroup.user_id == user_id)
    out = {}
    for leg in q.all():
        key = (leg.tradingsymbol, leg.product)
        out[key] = out.get(key, 0) + leg.quantity
    return out


# ----- marking -----------------------------------------------------------
# A leg's P&L is two numbers that add up:
#
#   settled  what completed position cycles already made. Banked when a position
#            closes, so it survives Kite dropping the row the next day, and
#            correctable by hand afterwards.
#   open     what the position currently held is doing, marked live.
#
# They coexist because the same contract can be closed and opened again: the
# first cycle's result stays banked while the new one runs, and the leg shows the
# combined figure. `state` describes the *current* position only, which is what
# decides whether the settled part may be edited.

def banked_of(leg):
    """The automatically accumulated total of completed cycles."""
    return float(getattr(leg, 'settled_pnl', 0.0) or 0.0)


def settled_of(leg):
    """A leg's settled P&L, honouring a correction the user made.

    A correction is not a permanent replacement: it fixes the total *as it stood*
    when it was typed, and cycles completed afterwards still add on top. So a leg
    corrected to ₹2,000 that later banks another ₹325 shows ₹2,325 — otherwise
    correcting one bad fill would quietly discard every trade after it.
    """
    override = getattr(leg, 'settled_override', None)
    if override is None:
        return banked_of(leg)
    since = banked_of(leg) - float(getattr(leg, 'settled_base', 0.0) or 0.0)
    return float(override) + since


def has_settled(leg, live):
    """Has anything on this leg actually settled?

    True once a position cycle has closed on it, or the user has corrected the
    figure by hand. Distinct from "settled to 0.00": a leg that has never been
    closed has no settled P&L at all, and saying ₹0.00 would assert something
    untrue. A cycle that has closed but not yet been banked counts.
    """
    if getattr(leg, 'settled_override', None) is not None:
        return True
    if int(getattr(leg, 'cycles', 0) or 0) > 0:
        return True
    return bool(getattr(leg, 'cycle_open', False)) and leg_state(live) == CLOSED


def pending_cycle_pnl(leg, live):
    """A finished cycle's amount that hasn't been banked yet.

    Banking is a write, so it happens on the poller's cycle and on a page load —
    but the figure must be right the instant a position closes, before either.
    Marking therefore adds the pending amount itself, which makes banking purely
    a persistence step: the total is identical either side of it. Without this a
    group read as 0 on the tick it closed, and the poller could trip a stoploss
    on that.
    """
    if not getattr(leg, 'cycle_open', False):
        return 0.0                       # nothing running, nothing to settle
    if live is not None and live['quantity']:
        return 0.0                       # still open — that's the live mark's job
    if live is not None:
        return auto_closed_pnl(leg, live)
    return float(getattr(leg, 'last_mark_pnl', 0.0) or 0.0)


def open_pnl(leg, live):
    """Live mark of the position this leg currently holds. 0 when it holds none."""
    if live is None or not live['quantity']:
        return 0.0
    return leg.quantity * (live['last_price'] - live['average_price'])


OPEN = 'open'
CLOSED = 'closed'


def leg_state(live):
    """``open`` when a position is held, ``closed`` otherwise.

    A leg is closed whether Kite still reports the squared-off row or has dropped
    it from the book entirely — from the group's point of view those are the same
    thing: nothing is running, and the settled figure is the leg's P&L. There is
    deliberately no third state, so anything a user can see and act on is one of
    two words.
    """
    if live is None or not live['quantity']:
        return CLOSED
    return OPEN


def leg_settled(leg, live):
    """Everything settled on this leg: banked, plus a close not yet banked."""
    return settled_of(leg) + pending_cycle_pnl(leg, live)


def leg_pnl(leg, live):
    """``(pnl, state)`` for one leg — settled plus live, so a re-opened contract
    carries its history rather than starting from zero.
    """
    return leg_settled(leg, live) + open_pnl(leg, live), leg_state(live)


def auto_closed_pnl(leg, live):
    """This group's pro-rata share of a squared-off position's settled P&L.

    Kite does *not* put the settled amount in ``realised`` for a carry-forward
    leg that was closed out — on a real squared-off NRML position ``realised``
    stays 0 while ``pnl`` (and ``unrealised``) carry the figure, which is
    ``sell_value - buy_value``. So take ``pnl``, which is Kite's own total for
    the row either way, and fall back to ``realised`` only if it is the one
    populated.

    The share is against the size the position actually held for *this* cycle
    (``basis_quantity``), falling back to the size recorded when the leg was
    tagged. A second cycle can be a different size from the first, so the
    original figure is the wrong divisor once a contract has been re-opened.
    """
    basis = abs(live.get('basis_quantity') or 0) or abs(leg.source_quantity or 0)
    share = (abs(leg.quantity) / basis) if basis else 0.0
    settled = live.get('pnl')
    if not settled:
        settled = live.get('realised') or 0.0
    return settled * share


def set_settled_pnl(db, leg, value, state):
    """Correct (or clear) a leg's settled P&L. Returns an error string or None.

    Refused while the position is open: that part of the figure is marked from
    the live price and would be overwritten on the next tick. A re-opened leg is
    therefore locked until it closes again — the settled history is only editable
    when nothing is running against it.

    The correction is recorded against the banked total it was typed over, so
    later cycles add to it instead of being swallowed. Clearing it restores the
    automatic figure.
    """
    if state == OPEN:
        return (f"{leg.tradingsymbol} still holds an open position — that part of its "
                f"P&L is marked live and cannot be set by hand. Close it first.")
    if value is None:
        leg.settled_override = None
        leg.settled_base = None
    else:
        leg.settled_override = float(value)
        leg.settled_base = banked_of(leg)
        # The typed figure is what the user saw, which already included any
        # just-closed cycle waiting to be banked. Close that cycle out here so
        # banking can't add it a second time.
        leg.cycle_open = False
        leg.last_mark_pnl = None
    db.commit()
    return None


def bank_settled(db, marks):
    """Move finished cycles into each leg's banked P&L. Returns how many banked.

    Called by the poller every cycle, and by Group Management when its owner
    loads the page. Two things happen per leg:

    * while a position is open, remember the live mark and that a cycle is
      running;
    * the first time it is seen not-open, add that cycle's settled amount to the
      bank and close the cycle out.

    Banking from the live row is preferred; if Kite dropped the row before we
    ever saw it at quantity 0 (a poll missed over a weekend, say) the last
    remembered mark is banked instead, so the number is never simply lost.
    Because the cycle flag is cleared as it banks, a re-opened contract starts a
    fresh cycle and is banked again on its own close.
    """
    banked, dirty = 0, False
    for mark in marks:
        for item in mark['legs']:
            leg, live, state = item['leg'], item['live'], item['state']
            if state == 'open':
                # Remembered every tick: this is what gets banked if the row
                # vanishes before we see it squared off, so it has to be
                # committed even on a call where nothing banks.
                if not leg.cycle_open:
                    leg.cycle_open = True
                    dirty = True
                if leg.last_mark_pnl != item['open_pnl']:
                    leg.last_mark_pnl = item['open_pnl']
                    dirty = True
                continue
            if not leg.cycle_open:
                continue
            # Exactly what marking was already counting as pending, so the total
            # doesn't move as it lands.
            leg.settled_pnl = banked_of(leg) + pending_cycle_pnl(leg, live)
            leg.cycle_open = False
            leg.last_mark_pnl = None
            leg.cycles = int(leg.cycles or 0) + 1
            banked += 1
            dirty = True
    if dirty:
        db.commit()
    return banked


def mark_group(db, group, live_map):
    """Value a group against the live book.

    Returns a dict with the group, its total P&L, per-leg detail, and counts —
    the single shared marking path for both the UI and the poller.
    """
    detail = []
    total = 0.0
    open_legs = 0
    for leg in legs_of(db, group.id):
        live = live_map.get((leg.tradingsymbol, leg.product))
        state = leg_state(live)
        banked = leg_settled(leg, live)
        running = open_pnl(leg, live)
        pnl = banked + running
        total += pnl
        if state == 'open':
            open_legs += 1
        detail.append({
            'leg': leg,
            'live': live,
            'pnl': pnl,
            # The two halves, so the UI can show what is banked separately from
            # what is still moving — and let the banked half be corrected.
            'settled': banked,
            'open_pnl': running,
            'has_settled': has_settled(leg, live),
            'cycles': int(getattr(leg, 'cycles', 0) or 0),
            'state': state,
            'overridden': getattr(leg, 'settled_override', None) is not None,
            'last_price': live['last_price'] if live else None,
            # Kite zeroes average_price once a position is squared off, so fall
            # back to what the leg was tagged at rather than showing 0.00.
            'average_price': (live['average_price'] if live and live['average_price']
                              else leg.avg_price),
            'position_quantity': live['quantity'] if live else None,
        })
    return {
        'group': group,
        'pnl': total,
        'legs': detail,
        'n_legs': len(detail),
        'open_legs': open_legs,
    }


def mark_all(db, maps_by_user, groups=None):
    """Mark each group against its own account's book.

    ``maps_by_user`` is ``{user_id: {(symbol, product): position}}``; a group
    whose account has no snapshot marks against an empty book, which freezes its
    legs rather than valuing them at zero.
    """
    return [
        mark_group(db, g, maps_by_user.get(g.user_id, {}))
        for g in (groups if groups is not None else list_groups(db))
    ]


# ----- triggers ----------------------------------------------------------
def evaluate(group, pnl):
    """``(trigger_type, message)`` if the group breached a level, else ``(None, None)``.

    Both levels must be *crossed*, not merely touched — strictly greater than
    the target, strictly less than the stoploss. Resting exactly on a level is
    not a breach, so a 20,000 target stays quiet at 20,000 and fires at 20,001;
    a -6,100 stoploss stays quiet at -6,100 and fires at -6,101; and a +1,000
    stoploss (a profit floor) stays quiet at 1,000 and fires at 999.
    """
    if group.target is not None and pnl > group.target:
        return TARGET, (f"🎯 Target reached — P&L ₹{pnl:,.2f} has risen above the "
                        f"₹{group.target:,.2f} target.")
    if group.stoploss is not None and pnl < group.stoploss:
        return STOPLOSS, (f"🛑 Stoploss hit — P&L ₹{pnl:,.2f} has fallen below the "
                          f"₹{group.stoploss:,.2f} stoploss.")
    return None, None


def apply_marks(db, marks, on_trigger=None):
    """Record marked P&L and fire any newly breached triggers.

    Called by the poller each cycle. Only deployed, alert-enabled groups can
    trigger, and each fires once: the group flips to ``triggered`` and stamps
    ``notified_at``, so a group sitting past its level does not re-alert every
    ten seconds. Returns the list of groups that tripped on this pass.
    """
    now = datetime.datetime.utcnow()
    fired = []
    for mark in marks:
        group, pnl = mark['group'], mark['pnl']
        group.last_pnl = pnl
        group.last_evaluated_at = now
        if group.status != DEPLOYED or not group.alert_enabled:
            continue
        trigger_type, message = evaluate(group, pnl)
        if not trigger_type:
            continue
        group.status = TRIGGERED
        group.trigger_type = trigger_type
        group.trigger_message = message
        group.triggered_at = now
        group.triggered_pnl = pnl
        group.notified_at = now
        fired.append((group, pnl, trigger_type, message))
    db.commit()
    # Notify only after the commit, so a slow or failing send cannot lose the
    # fact that the group tripped.
    if on_trigger:
        for group, pnl, trigger_type, message in fired:
            on_trigger(group, pnl, trigger_type, message)
    return [f[0] for f in fired]


def monitored(db):
    """Groups the poller needs to value: deployed or already triggered."""
    return (
        db.query(TradeGroup)
        .filter(TradeGroup.status.in_([DEPLOYED, TRIGGERED]))
        .all()
    )


def accounts_with_groups(db):
    """User ids that actually own a monitored group — the poller's fetch list."""
    return sorted({g.user_id for g in monitored(db) if g.user_id})


# ----- lifecycle ---------------------------------------------------------
def deploy(db, group, lot_sizes=None):
    """Arm a group for monitoring. Returns ``(ok, error)``.

    Re-checks every leg's quantity rather than trusting what was stored: a leg
    saved before the whole-lot rule existed, or while the lot size could not be
    resolved, must not slip into a monitored group.
    """
    legs = legs_of(db, group.id)
    if not legs:
        return False, f"'{group.name}' has no positions — add at least one before deploying."
    if group.stoploss is None and group.target is None:
        return False, f"'{group.name}' needs a stoploss or a target before deploying."
    err = validate_levels(group.stoploss, group.target)
    if err:
        return False, err

    problems = [
        p for p in (
            validate_lot_multiple(leg.quantity, leg.tradingsymbol,
                                  (lot_sizes or {}).get(leg.tradingsymbol))
            for leg in legs
        ) if p
    ]
    if problems:
        return False, (f"Can't deploy '{group.name}' — "
                       + " ".join(problems))
    group.status = DEPLOYED
    group.deployed_at = datetime.datetime.utcnow()
    group.trigger_type = None
    group.trigger_message = None
    group.triggered_at = None
    group.triggered_pnl = None
    group.notified_at = None
    db.commit()
    return True, None


def undeploy(db, group):
    """Return a group to draft, clearing any trigger state."""
    group.status = DRAFT
    group.trigger_type = None
    group.trigger_message = None
    group.triggered_at = None
    group.triggered_pnl = None
    group.notified_at = None
    db.commit()
