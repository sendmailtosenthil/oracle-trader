"""Group definition, P&L marking, and trigger evaluation.

A group's P&L is the sum of its legs, each marked against the *position's
average price* and pro-rated to the quantity the group owns:

    leg_pnl = group_qty * (last_price - average_price)

so a group holding a whole position reports exactly the P&L Kite shows, and a
group holding half of it reports half. Once the broker squares the position off
(``quantity == 0``) the leg's share of the realised P&L is frozen instead.

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


def legs_of(db, group_id):
    return (
        db.query(TradeGroupLeg)
        .filter(TradeGroupLeg.group_id == group_id)
        .order_by(TradeGroupLeg.tradingsymbol)
        .all()
    )


def create_group(db, name, user_id, stoploss=None, target=None, alert_enabled=True):
    """Create a draft group owned by ``user_id``. Returns ``(group, error)``."""
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
    group = TradeGroup(
        name=name, user_id=user_id, stoploss=stoploss, target=target,
        alert_enabled=bool(alert_enabled), status=DRAFT,
    )
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


def update_group(db, group, name=None, stoploss=..., target=..., alert_enabled=None):
    """Patch a group's editable fields. Returns ``(group, error)``.

    ``stoploss`` / ``target`` use an ``...`` sentinel so ``None`` can be passed
    explicitly to disarm that side.
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
    if alert_enabled is not None:
        group.alert_enabled = bool(alert_enabled)
    db.commit()
    return group, None


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
    pos_qty = int(position['quantity'])
    if pos_qty == 0:
        return None, f"{position['tradingsymbol']} is closed (qty 0) — nothing to add."
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
def leg_pnl(leg, live):
    """``(pnl, state)`` for one leg. State is ``open`` / ``closed`` / ``missing``.

    An open leg is always marked live. Once it is no longer open its P&L is
    settled, and ``frozen_pnl`` — captured automatically when it closes, and
    editable afterwards — wins over the derived figure. That override matters
    because splitting a squared-off position's realised P&L across groups is
    only a pro-rata guess; the user knows what actually filled.
    """
    if live is None:
        return (leg.frozen_pnl or 0.0), 'missing'
    if live['quantity'] == 0:
        if leg.frozen_pnl is not None:
            return leg.frozen_pnl, 'closed'
        return auto_closed_pnl(leg, live), 'closed'
    return leg.quantity * (live['last_price'] - live['average_price']), 'open'


def auto_closed_pnl(leg, live):
    """This group's pro-rata share of a squared-off position's realised P&L."""
    basis = abs(leg.source_quantity or 0)
    share = (abs(leg.quantity) / basis) if basis else 0.0
    return (live.get('realised') or 0.0) * share


def set_closed_pnl(db, leg, value, state):
    """Override (or clear) a closed leg's P&L. Returns an error string or None.

    Only meaningful once the leg has stopped moving: an open leg is marked
    from the live price, so a hand-set value would be overwritten next tick.
    """
    if state == 'open':
        return (f"{leg.tradingsymbol} is still open — its P&L is marked live and "
                f"cannot be set by hand. Close the position first.")
    leg.frozen_pnl = None if value is None else float(value)
    db.commit()
    return None


def capture_closed_pnl(db, marks):
    """Freeze the P&L of legs that have just stopped being open.

    Called by the poller. Kite keeps revising ``realised`` after a square-off,
    so pinning the value at the moment the leg closed stops a settled group
    drifting — and gives the user a concrete number to correct.
    """
    frozen = 0
    for mark in marks:
        for item in mark['legs']:
            leg = item['leg']
            if item['state'] != 'open' and leg.frozen_pnl is None:
                leg.frozen_pnl = item['pnl']
                frozen += 1
    if frozen:
        db.commit()
    return frozen


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
        pnl, state = leg_pnl(leg, live)
        total += pnl
        if state == 'open':
            open_legs += 1
        detail.append({
            'leg': leg,
            'live': live,
            'pnl': pnl,
            'state': state,
            'overridden': state != 'open' and leg.frozen_pnl is not None,
            'last_price': live['last_price'] if live else None,
            'average_price': live['average_price'] if live else leg.avg_price,
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
    """``(trigger_type, message)`` if the group breached a level, else ``(None, None)``."""
    if group.target is not None and pnl >= group.target:
        return TARGET, (f"🎯 Target reached — P&L ₹{pnl:,.2f} is at or above the "
                        f"₹{group.target:,.2f} target.")
    if group.stoploss is not None and pnl <= group.stoploss:
        return STOPLOSS, (f"🛑 Stoploss hit — P&L ₹{pnl:,.2f} is at or below the "
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
