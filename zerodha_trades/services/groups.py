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

A group says who owns those two levels through ``levels_mode``. ``fixed`` is the
original behaviour — the user types them and nothing else touches them. ``auto``
hands them to the app, which manages them from the premium the basket was opened
for; see "automatic levels" below.
"""
import datetime
import math

from common.database import (TradeGroup, TradeGroupLeg, TradeGroupLevelEvent,
                             TradeGroupSetting)

DRAFT = 'draft'
DEPLOYED = 'deployed'
TRIGGERED = 'triggered'

TARGET = 'TARGET'
STOPLOSS = 'STOPLOSS'

FIXED = 'fixed'
AUTO = 'auto'
LEVELS_MODES = (FIXED, AUTO)
MODE_LABELS = {FIXED: "Fixed SL & target", AUTO: "Auto-adjust SL & target"}


def levels_mode_of(group):
    """A group's levels mode, defaulting to ``fixed``.

    Read through a helper rather than off the column: every group that predates
    the mode has NULL there, and those were all typed by hand.
    """
    mode = (getattr(group, 'levels_mode', None) or '').strip().lower()
    return mode if mode in LEVELS_MODES else FIXED


def is_auto(group):
    return levels_mode_of(group) == AUTO


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
                 owner=None, shared=False, levels_mode=FIXED, threshold=None):
    """Create a draft group on Zerodha account ``user_id``. Returns ``(group, error)``.

    ``channels`` is the notification channels to use (see
    :mod:`zerodha_trades.services.alerts`); ``None`` means all of them. An
    empty list is how a group is created silent.

    ``owner`` is the *app* user creating it — distinct from ``user_id``, which
    is the Kite login the positions belong to. ``shared`` opens the group up for
    other users to view; it never lets them edit.

    ``levels_mode`` decides who owns the levels. On ``auto`` any ``stoploss`` /
    ``target`` passed in is ignored rather than kept: the group's levels are
    derived, so a typed one would be overwritten on the next recompute and is
    better refused outright than silently honoured for a while. ``threshold`` is
    the smallest stoploss step auto mode will take, defaulting to
    :data:`DEFAULT_THRESHOLD`.
    """
    name = (name or '').strip()
    if not name:
        return None, "Group name is required."
    if not user_id:
        return None, "A group must belong to a Zerodha account."
    mode = levels_mode if levels_mode in LEVELS_MODES else FIXED
    if mode == AUTO:
        stoploss = target = None
    clash = db.query(TradeGroup).filter(TradeGroup.name == name).first()
    if clash:
        # Names are unique across accounts, so say which one already has it
        # rather than leave the user guessing at an invisible collision.
        return None, (f"A group named '{name}' already exists"
                      + (f" under {clash.user_id}." if clash.user_id != user_id else "."))
    err = validate_levels(stoploss, target)
    if err:
        return None, err
    if threshold is not None and float(threshold) <= 0:
        return None, "The stoploss step must be more than ₹0."
    group = TradeGroup(name=name, user_id=user_id, stoploss=stoploss,
                       target=target, levels_mode=mode, status=DRAFT, owner=owner,
                       shared=bool(shared),
                       auto_threshold=(float(threshold) if threshold
                                       else DEFAULT_THRESHOLD))
    apply_channels(group, ALL_CHANNELS if channels is None else channels)
    db.add(group)
    db.commit()
    refresh_auto_levels(db, group)
    return group, None


# ----- automatic levels ---------------------------------------------------
# An auto group manages its own levels from a single figure: the profit the
# basket makes if every contract expires worthless — the premium it was opened
# for, priced from the legs as they were tagged. Call it E.
#
#   opening stoploss   -E, rounded to the rupee. The whole premium is what the
#                      trade is risking to keep half of it.
#   opening target      50% of E.
#   the ratchet         each tick, the stoploss rises by whatever the P&L has
#                      gained since the stoploss was last set — but only once
#                      that step is worth at least `threshold` rupees, so the
#                      level moves in meaningful jumps instead of every tick.
#                      It only ever goes up, and is recorded when it does.
#   target reached      notify on every channel, drop the stoploss to break-even
#                      (0), and move the target up the ladder — 50%, then 70%,
#                      then 85% of E. Reaching the last rung ends the ladder and
#                      advises closing the trade; the ratcheting stoploss is the
#                      only exit left for anyone who holds on.
#   legs change         a leg closing fixes its result and a new leg brings its
#                      own premium, so E is re-priced (see :func:`_refit_basis`)
#                      and the target re-derived at the rung the group is on. The
#                      stoploss takes the new opening level only if it tightens.
#
# Typing over either level takes the group off automatic entirely: it becomes a
# fixed-levels group and nothing derived touches it again. That is a mode change,
# not an exception to the rules below — see :func:`update_group`.
#
# Two invariants hold for as long as a group stays armed:
#
#   the risk at the stoploss only ever falls.  The level moves up and never
#       down — a loss of one premium becomes a smaller loss, then break-even,
#       then a locked-in profit. Enforced in one place,
#       :func:`tighten_stoploss`, which is the only writer of an automatic stop,
#       so no caller can loosen one by forgetting to check.
#   stoploss <= auto_anchor_pnl.  The level never passes the profit that set it,
#       so an adjustment cannot stop the group out on the tick it happens.
#
# The one place a *wider* stoploss appears is arming: deploying (or re-arming)
# derives a fresh opening level from the legs, because that is a new commitment
# by the user — to a basket whose legs may have changed while it sat in draft,
# and, after a stop-out, on a level that has already fired and would fire again.
# That reset is recorded like everything else, so it is visible rather than
# silent.
#
# Every move is written to ztrade_group_level_events, which is the stoploss
# journey the dashboard draws.

# Smallest stoploss step worth taking, in rupees. Per group, because it is a
# judgement about that basket's size, not a property of the market.
DEFAULT_THRESHOLD = 300.0

# Targets, as fractions of the expected profit, in the order they are taken.
# Reaching the last one is as far as the ladder goes: what follows is advice to
# close the trade, because holding for the final slice of a premium risks the
# whole of what the ladder has already earned.
TARGET_FRACTIONS = (0.5, 0.7, 0.85)

# Level-event kinds. `armed` opens a journey and `disarmed` closes it, so a
# group deployed twice reads as two journeys rather than one confusing line.
ARMED = 'armed'
SL_ADJUSTED = 'sl_adjusted'
TARGET_REACHED = 'target_reached'
BASIS_CHANGED = 'basis_changed'
MANUAL = 'manual'
DISARMED = 'disarmed'


def threshold_of(group):
    """The smallest stoploss step this group will take, in rupees."""
    value = getattr(group, 'auto_threshold', None)
    return float(value) if value else DEFAULT_THRESHOLD


def is_option_symbol(symbol):
    """True for an NFO/BFO option contract, told by its CE/PE suffix.

    Suffix rather than the instruments master on purpose: this is needed inside
    the poller, which deliberately never loads the instruments dump — it is the
    single biggest thing that would cost the VPS memory on every cycle.
    """
    return (symbol or '').upper().endswith(('CE', 'PE'))


def premium_of(leg):
    """What one leg was opened for, positive when premium was received.

    A short leg carries a negative quantity, so ``-quantity * avg_price`` is the
    credit taken in; a long leg comes out negative, the debit paid.
    """
    return -leg.quantity * float(leg.avg_price or 0.0)


def auto_basis(db, group, live_map=None):
    """``(expected_profit, problem)`` — the figure every auto level derives from.

    A leg contributes what it is *expected to be worth on expiry day*, and that
    depends on whether it is still running:

    * **open** — its premium. What the leg keeps if the contract expires
      worthless, priced at the average the leg was tagged at.
    * **closed** — its settled P&L. The position is gone, so its result is a
      fixed rupee figure and no longer an expectation at all. A leg closed at a
      loss therefore *reduces* what the group can still make, which is exactly
      what the levels should be measured against.

    So the basis is not a constant for the life of a group: closing a leg or
    tagging another one moves it, and :func:`_refit_basis` re-prices the levels
    when it does.

    ``live_map`` is the position book to judge open/closed against; without one
    the poller's snapshot is read, so a caller with no book of its own still gets
    the right answer rather than a stale guess. With a book in hand a *missing*
    row means the leg is closed — Kite drops a squared-off position. With no book
    at all it means nothing, and a leg counts as it was tagged: writing a live
    trade off as closed would price the basis off a book nobody has looked at.

    A ``problem`` string means the basis cannot be established, and says why in
    words the UI can show as-is:

    * no legs yet — nothing has been traded to expect a profit from;
    * an open leg that is not an option, where premium says nothing about an
      expiry value (a future's ``quantity * price`` is notional, not a credit);
    * nothing left to keep — the open premium and the settled results net out to
      zero or a debit, which has no profit to take fractions of.
    """
    legs = legs_of(db, group.id)
    if not legs:
        return None, ("no positions tagged yet — automatic levels come from the "
                      "premium of the trades in the group.")
    book = live_map if live_map is not None else _snapshot_book(db, group)
    running, settled = [], []
    for leg in legs:
        live = book.get((leg.tradingsymbol, leg.product))
        (settled if _basis_closed(leg, live, bool(book)) else running).append(
            (leg, live))

    # Only the open legs need to be options: a closed one contributes rupees, and
    # what kind of contract earned them no longer matters.
    others = sorted({leg.tradingsymbol for leg, _ in running
                     if not is_option_symbol(leg.tradingsymbol)})
    if others:
        return None, (f"{', '.join(others[:3])} is not an option, so this group has "
                      f"no expiry premium to work from. Use fixed levels for it.")
    # To the paisa: prices carry two decimals, and float noise a thousandth of a
    # rupee wide would otherwise cost a whole rupee when the stoploss is floored.
    expected = round(sum(premium_of(leg) for leg, _ in running)
                     + sum(leg_settled(leg, live) for leg, live in settled), 2)
    if expected <= 0:
        return None, (f"the open premium and the closed legs net out to "
                      f"₹{expected:,.2f}, so there is nothing left to keep. Use "
                      f"fixed levels for it.")
    return expected, None


def _basis_closed(leg, live, have_book):
    """Whether the basis should treat this leg as a settled rupee figure.

    A live row decides it outright. Without one it comes down to whether there is
    a book to be missing from: inside a real book a dropped row *is* the closure,
    but with nothing polled for the account the leg is only closed if it has
    actually banked a cycle — otherwise every leg of a fresh draft group would be
    written off as closed and the group would look like it had nothing to make.
    """
    if live is not None:
        return leg_state(live) == CLOSED
    if have_book:
        return True
    return (int(getattr(leg, 'cycles', 0) or 0) > 0
            or getattr(leg, 'settled_override', None) is not None)


def _snapshot_book(db, group):
    """The poller's last position book for a group's account.

    Imported inside the call, like the other cross-service reach in this module:
    it keeps the marking core importable on its own, which is what lets the payoff
    maths depend on it without dragging the broker client in.
    """
    from zerodha_trades.services import positions
    return positions.snapshot_maps(db).get(group.user_id, {})


def auto_levels(db, group, live_map=None):
    """The opening levels for an auto group, as ``(stoploss, target, expected)``.

    All three are ``None`` when the basis cannot be established. The stoploss is
    rounded *down* to the rupee (a wider stop, never a tighter one) and the target
    to the nearest rupee.
    """
    expected, problem = auto_basis(db, group, live_map)
    if problem:
        return None, None, None
    return (float(math.floor(-expected)),
            float(round(TARGET_FRACTIONS[0] * expected)),
            float(expected))


def refresh_auto_levels(db, group, live_map=None, commit=True, force=False):
    """Re-derive an auto group's opening levels. Returns whether anything moved.

    Only while the group is *not* armed. A deployed group is running a journey:
    its stoploss has been ratcheted, its target may have been taken, and both are
    now facts about the trade rather than a function of the legs — so adding a leg
    to a live group does not rebase them. Arming, re-arming and undeploying all go
    through here, which is where a fresh journey starts from the legs as they are.

    ``force`` is for the one case that is a fresh start without a status change:
    a group switched from fixed to auto while it is deployed.

    A no-op on a fixed group: those levels are the user's, and nothing derived
    may overwrite them.
    """
    if not is_auto(group):
        return False
    if group.status == DEPLOYED and not force:
        return False
    stoploss, target, expected = auto_levels(db, group, live_map)
    before = (group.stoploss, group.target, group.auto_expected_profit)
    group.stoploss = stoploss
    group.target = target
    group.auto_expected_profit = expected
    group.auto_anchor_pnl = 0.0      # the opening stop was set at zero profit
    group.auto_target_stage = 0
    if commit:
        db.commit()
    return before != (stoploss, target, expected)


def next_target(group, stage):
    """The target for a group that has reached ``stage`` targets, or ``None``.

    ``None`` once every fraction has been taken: the ladder ends there, and what
    follows is advice to close rather than another level.
    """
    expected = getattr(group, 'auto_expected_profit', None)
    if expected is None or stage >= len(TARGET_FRACTIONS):
        return None
    return float(round(TARGET_FRACTIONS[stage] * float(expected)))


CLOSE_ADVICE = ("🏁 Close the trade — every target has been reached and what is "
                "left of the premium is not worth the profit already made.")


def all_targets_taken(group):
    """True once an auto group has climbed the whole ladder."""
    return (is_auto(group)
            and int(getattr(group, 'auto_target_stage', 0) or 0) >= len(TARGET_FRACTIONS))


def target_label(group):
    """Which target an auto group is working toward, e.g. ``"50% of ₹12,000"``."""
    stage = int(getattr(group, 'auto_target_stage', 0) or 0)
    expected = getattr(group, 'auto_expected_profit', None)
    if expected is None:
        return "no expected profit established"
    if stage >= len(TARGET_FRACTIONS):
        return (f"all {len(TARGET_FRACTIONS)} targets taken "
                f"({TARGET_FRACTIONS[-1] * 100:.0f}% of ₹{expected:,.2f}) — "
                f"closing the trade is advised; the trailing stoploss is the only "
                f"exit left")
    return (f"{TARGET_FRACTIONS[stage] * 100:.0f}% of the ₹{expected:,.2f} "
            f"expected profit")


# ----- the level journey --------------------------------------------------
def record_level_event(db, group, kind, pnl=None, note=None, at=None,
                       commit=False):
    """Append one row to a group's level history.

    Left uncommitted by default: the poller lands a whole cycle — snapshots,
    banking, marks and these — in one transaction, so a crash cannot record a
    level that was never actually set.
    """
    event = TradeGroupLevelEvent(
        group_id=group.id,
        at=at or datetime.datetime.utcnow(),
        kind=kind,
        stoploss=group.stoploss,
        target=group.target,
        pnl=pnl,
        expected_profit=getattr(group, 'auto_expected_profit', None),
        note=note,
    )
    db.add(event)
    if commit:
        db.commit()
    return event


def level_events(db, group_id, limit=None):
    """A group's level history, oldest first."""
    query = (db.query(TradeGroupLevelEvent)
             .filter(TradeGroupLevelEvent.group_id == group_id)
             .order_by(TradeGroupLevelEvent.at.asc(),
                       TradeGroupLevelEvent.id.asc()))
    if limit:
        # The tail is the interesting end, so take the last N and re-order.
        rows = (query.order_by(None)
                .order_by(TradeGroupLevelEvent.at.desc(),
                          TradeGroupLevelEvent.id.desc())
                .limit(limit).all())
        return list(reversed(rows))
    return query.all()


def has_level_events(db, group_id):
    """Whether a group has any recorded level history.

    What the journey view is offered on, rather than the group being on auto right
    now: a group taken over by hand keeps the history of how its levels got there,
    and hiding it would lose the record at the moment it explains the most.
    """
    return (db.query(TradeGroupLevelEvent.id)
            .filter(TradeGroupLevelEvent.group_id == group_id)
            .first() is not None)


def delete_level_events(db, group_id):
    """Drop a group's level history. Called when the group itself goes."""
    return (db.query(TradeGroupLevelEvent)
            .filter(TradeGroupLevelEvent.group_id == group_id)
            .delete())


# ----- the ratchet -------------------------------------------------------
def advance_auto(db, group, pnl, live_map=None, now=None):
    """Move an armed auto group's levels for this tick.

    Returns the notifications the caller owes, as ``[(trigger_type, message)]``.
    Only a target earns one: the stoploss steps are bookkeeping, and a message
    every few minutes as it trails would train the user to ignore the channel
    that also carries the stop-out.

    Writes are left uncommitted for the caller's transaction (see
    :func:`record_level_event`).
    """
    if not is_auto(group) or group.status != DEPLOYED:
        return []
    now = now or datetime.datetime.utcnow()
    # The basis first: a leg that closed or was added changes what this trade can
    # still make, and both the target and the stop have to be measured against
    # the new figure before this tick's step is worked out.
    _refit_basis(db, group, pnl, live_map, now)
    _ratchet_stoploss(db, group, pnl, now)
    notify = []
    # A loop, not an `if`: a tick that gaps straight through two rungs has reached
    # both, and each is its own recorded event and its own alert.
    while group.target is not None and pnl > group.target:
        notify.append(_reach_target(db, group, pnl, now))
    return notify


def _refit_basis(db, group, pnl, live_map, now):
    """Re-price the levels when the group's legs have changed under them.

    A leg closing fixes its result, and a new leg brings its own premium, so the
    expected profit moves. When it does:

    * the **target** is re-derived at the rung the group has reached — the ladder
      is a fraction of what the trade can make, so a bigger basket earns a bigger
      target and a smaller one a smaller target;
    * the **stoploss** is offered the new opening level, and takes it only if it
      *tightens*. Adding legs widens that level, and a stop never goes backwards
      — the risk this trade has already taken off the table stays off it.

    The offered level is also capped at the current P&L, so a basis that has
    collapsed cannot stop the group out by arithmetic on the tick it is
    recalculated; the level sits at the P&L and any further loss trips it.
    """
    expected, problem = auto_basis(db, group, live_map)
    if problem or expected is None:
        return False
    if group.auto_expected_profit is not None \
            and abs(expected - float(group.auto_expected_profit)) < 0.005:
        return False
    was = group.auto_expected_profit
    group.auto_expected_profit = expected
    group.target = next_target(group, int(group.auto_target_stage or 0))
    moved = tighten_stoploss(group, min(float(math.floor(-expected)),
                                        float(math.floor(pnl))))
    if moved:
        group.auto_anchor_pnl = pnl
    record_level_event(
        db, group, BASIS_CHANGED, pnl=pnl, at=now,
        note=(f"Trade legs changed — expected profit "
              f"{'—' if was is None else f'₹{was:,.2f}'} → ₹{expected:,.2f}; "
              f"target re-derived"
              + ("; stoploss tightened" if moved else "; stoploss held")))
    return True


def tighten_stoploss(group, value):
    """Move the stoploss to ``value`` — but only if that *reduces* what is at risk.

    The single writer of an automatic stoploss, so the direction is a property of
    the code rather than of every caller remembering it. A stop walks
    ``-5,000 → -4,700 → -4,000 → 0 → 300 → 1,000``: each step gives up less of the
    trade, and a level that would hand risk back is refused outright.
    """
    if value is None:
        return False
    if group.stoploss is not None and value <= group.stoploss:
        return False
    group.stoploss = float(value)
    return True


def _ratchet_stoploss(db, group, pnl, now):
    """Lift the stoploss by what the P&L has gained since it was last set."""
    if group.stoploss is None:
        return False
    gain = pnl - float(group.auto_anchor_pnl or 0.0)
    # Rounded *down* to the rupee. Rounding up could put the level as much as 50
    # paise above the profit that justified it, which would stop the group out on
    # the very tick that moved the stop — the one outcome a trailing stop must
    # never produce.
    candidate = float(math.floor(group.stoploss + gain))
    # The spec's own test: the step itself has to be worth taking. Measured after
    # rounding, so a step that only clears the threshold before it waits for the
    # next tick rather than landing short.
    if candidate - group.stoploss < threshold_of(group):
        return False
    if group.target is not None and candidate >= group.target:
        # The target is being reached on this same tick; leave the stop where it
        # is and let :func:`_reach_target` place it, rather than push it above the
        # level that is about to be taken.
        return False
    before = group.stoploss
    if not tighten_stoploss(group, candidate):
        return False
    group.auto_anchor_pnl = pnl
    record_level_event(db, group, SL_ADJUSTED, pnl=pnl, at=now,
                       note=(f"Trailing step +₹{candidate - before:,.2f} "
                             f"(₹{before:,.2f} → ₹{candidate:,.2f})"))
    return True


def _reach_target(db, group, pnl, now):
    """Take a target: lock the stop at break-even, move the target on, notify."""
    reached = group.target
    stage = int(group.auto_target_stage or 0) + 1
    # Break-even — which by the first target is a jump up from a stop still in
    # loss. At a later target the ratchet already has it above zero, and
    # :func:`tighten_stoploss` refuses to bring it back down: profit the trade has
    # banked into the level is not handed back.
    tighten_stoploss(group, 0.0)
    group.auto_anchor_pnl = pnl
    group.auto_target_stage = stage
    group.target = next_target(group, stage)
    if group.target is not None:
        moved = (f"the target moves to ₹{group.target:,.2f} "
                 f"({TARGET_FRACTIONS[stage] * 100:.0f}% of the expected profit).")
    else:
        moved = (f"That was the last rung of the ladder. {CLOSE_ADVICE} The "
                 f"trailing stoploss is the only exit left if you hold on.")
    message = (f"🎯 Target reached — P&L ₹{pnl:,.2f} has risen above the "
               f"₹{reached:,.2f} target. Stoploss is now ₹{group.stoploss:,.2f} "
               f"and {moved}")
    record_level_event(
        db, group, TARGET_REACHED, pnl=pnl, at=now,
        note=(f"Target {stage} of {len(TARGET_FRACTIONS)} hit "
              f"({TARGET_FRACTIONS[stage - 1] * 100:.0f}% = ₹{reached:,.2f})"
              + ("" if group.target is not None else " — close advised")))
    return TARGET, message


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


def levels_advisory(group):
    """The lopsided-levels warning for a group, or ``None``.

    Auto levels are lopsided by design — risk the whole premium to keep half of
    it — so there is nothing to flag: the shape *is* the strategy, and warning
    about it every save would be noise.
    """
    if is_auto(group):
        return None
    return levels_imbalance(group.stoploss, group.target)


def update_group(db, group, name=None, stoploss=..., target=..., channels=None,
                 shared=None, levels_mode=None, threshold=None):
    """Patch a group's editable fields. Returns ``(group, error)``.

    ``stoploss`` / ``target`` use an ``...`` sentinel so ``None`` can be passed
    explicitly to disarm that side. ``shared``, ``levels_mode`` and ``threshold``
    are left alone when ``None``.

    Switching to ``auto`` re-derives the levels: the ones it held were typed for a
    fixed group, and keeping them would leave hand-set numbers armed under a label
    promising computed ones. Switching to ``fixed`` — which is how a user takes an
    automatic group over by hand — keeps whatever the levels currently are, so the
    computed pair becomes the starting point for editing, and this call can pass
    new ones in the same breath.

    While a group *stays* on ``auto``, levels passed here are ignored rather than
    written: they belong to the ratchet. Note "ignored", not "blanked" — an armed
    group's levels are facts about a trade in flight, and clearing them would
    disarm it in the middle.
    """
    was = levels_mode_of(group)
    mode = levels_mode if levels_mode in LEVELS_MODES else was
    if mode == AUTO:
        stoploss = target = ...
    if threshold is not None and float(threshold) <= 0:
        return group, "The stoploss step must be more than ₹0."
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
    group.levels_mode = mode
    group.stoploss = new_sl
    group.target = new_tg
    if threshold is not None:
        group.auto_threshold = float(threshold)
    if channels is not None:
        apply_channels(group, channels)
    if shared is not None:
        group.shared = bool(shared)
    if was == AUTO and mode == FIXED:
        # The hand-over is part of the trade's level history: the journey stops
        # here, and the chart should show why it stops rather than simply end.
        record_level_event(db, group, MANUAL, pnl=group.last_pnl,
                           note="Levels set by hand — automatic adjustment off")
    db.commit()
    # Turning auto on is the start of a journey even mid-flight, so it re-derives
    # on a deployed group too — otherwise the switch would leave it armed with the
    # levels it just discarded.
    refresh_auto_levels(db, group, force=(mode == AUTO and was != AUTO))
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
    """Delete a group and everything recorded about it — legs and level history."""
    db.query(TradeGroupLeg).filter(TradeGroupLeg.group_id == group.id).delete()
    delete_level_events(db, group.id)
    db.delete(group)
    db.commit()


# ----- legs --------------------------------------------------------------
def add_leg(db, group, position, quantity=None, lot_size=None):
    """Tag a position (or a slice of it) into a group. Returns ``(leg, error)``.

    ``quantity`` defaults to the position's full quantity. On an *open* position
    it must be non-zero, point the same way, not exceed it in magnitude, and —
    for derivatives — be a whole number of lots. A *closed* one is only ever a
    settled figure, so the lot rules don't apply to it (see
    :func:`validate_leg_quantity`).
    """
    # A closed position is still taggable: it keeps its settled P&L in the
    # group. Validate against the size it had rather than today's zero.
    pos_qty = int(position.get('basis_quantity') or position['quantity'])
    if pos_qty == 0:
        return None, (f"{position['tradingsymbol']}: can't tell what size this "
                      f"position was — nothing to add.")
    qty = pos_qty if quantity is None else int(quantity)
    err = validate_leg_quantity(qty, pos_qty, position['tradingsymbol'], lot_size,
                               closed=not int(position.get('quantity') or 0))
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
    # The trades are what auto levels are derived from, so tagging one re-derives
    # them — the "first stoploss is set from the trades added to the group" rule.
    # A group already armed is left alone; see :func:`refresh_auto_levels`.
    refresh_auto_levels(db, group)
    return leg, None


def validate_leg_quantity(qty, pos_qty, symbol, lot_size=None, closed=False):
    """Leg quantity must be non-zero, same-signed, and within the position.

    On an open position it must also be a whole number of lots, and an
    unresolved ``lot_size`` is a *failure*, not a free pass: without it the
    whole-lot rule cannot be checked, and letting the quantity through unchecked
    is exactly how a bad one reaches a deployed group.

    ``closed`` drops both of those. A settled leg is a rupee figure and nothing
    more — its contract may have expired and its lot size may no longer resolve,
    neither of which changes what it made. The sign and magnitude checks stay,
    because they are about the closed trade's own size (what its P&L is
    pro-rated against), not about anything the broker is still reporting.
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
    if closed:
        return None
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
    """Change a leg's quantity, validated against the live position if known.

    A closed leg skips the lot rules: it holds a settled figure, so there is no
    live position for the quantity to be legal against and no reason to need its
    contract to still resolve.
    """
    qty = int(quantity)
    live = (live_map or {}).get((leg.tradingsymbol, leg.product))
    closed = leg_state(live) == CLOSED
    basis = int(live['quantity']) if live and live['quantity'] else leg.source_quantity
    err = validate_leg_quantity(qty, basis, leg.tradingsymbol, lot_size, closed=closed)
    if err:
        return err
    leg.quantity = qty
    db.commit()
    refresh_auto_levels(db, get_group(db, leg.group_id))
    return None


def remove_leg(db, leg):
    group = get_group(db, leg.group_id)
    db.delete(leg)
    db.commit()
    refresh_auto_levels(db, group)


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


def bank_settled(db, marks, commit=True):
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

    ``commit=False`` leaves the changes pending for a caller that is going to
    commit anyway — the poller, which then lands banking and marking in one
    transaction. That is strictly safer than two: a crash between them can no
    longer bank a cycle whose mark was never recorded.
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
    if dirty and commit:
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
    """Record marked P&L, move automatic levels, and fire any breached triggers.

    Called by the poller each cycle. Three things happen to a deployed group:

    1. its marked P&L is stored;
    2. an auto group's levels advance — the stoploss trails, a reached target is
       taken and moved on. This is level *management*, so it runs whether or not
       the group notifies: "monitored but silent" silences the messages, not the
       trailing stop;
    3. a breached level triggers. Each group fires once: it flips to
       ``triggered`` and stamps ``notified_at``, so one sitting past its level
       does not re-alert every ten seconds.

    An auto group's target is taken in step 2 and moved above the P&L there, so
    step 3 only ever sees its stoploss — the escalation and the terminal trigger
    cannot both fire on the same level.

    Returns the groups that tripped on this pass; targets taken along the way are
    notified but leave the group running.
    """
    now = datetime.datetime.utcnow()
    fired, taken = [], []
    for mark in marks:
        group, pnl = mark['group'], mark['pnl']
        group.last_pnl = pnl
        group.last_evaluated_at = now
        if group.status != DEPLOYED:
            continue
        # The book this group was just marked against, so the basis can tell an
        # open leg from a closed one without a second query.
        book = {(item['leg'].tradingsymbol, item['leg'].product): item['live']
                for item in mark['legs']}
        for trigger_type, message in advance_auto(db, group, pnl, book, now):
            if group.alert_enabled:
                group.notified_at = now
                taken.append((group, pnl, trigger_type, message))
        if not group.alert_enabled:
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
    # fact that the group tripped or that its levels moved.
    if on_trigger:
        for group, pnl, trigger_type, message in taken + fired:
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
def deploy(db, group, lot_sizes=None, baseline=None, live_map=None):
    """Arm a group for monitoring. Returns ``(ok, error)``.

    Re-checks every *open* leg's quantity rather than trusting what was stored: a
    leg saved before the whole-lot rule existed, or while the lot size could not
    be resolved, must not slip into a monitored group.

    Closed legs are exempt. One holds a settled rupee figure; its contract may
    have expired and its lot size may no longer resolve, neither of which changes
    what it made. Validating it would strand the group — a triggered basket whose
    legs have since been squared off could never be re-armed with new levels,
    which is precisely when you want to.

    ``baseline`` is ``{'spot', 'sigma', 'iv'}`` as of now — where the underlying
    stood and how far the market implied it could travel before the front
    expiry. Frozen here because deploying is the moment the group starts being
    monitored, and therefore the range the user actually accepted. Optional: a
    group whose spot cannot be established still deploys, just without a
    reference band on its chart.
    """
    legs = legs_of(db, group.id)
    if not legs:
        return False, f"'{group.name}' has no positions — add at least one before deploying."
    if is_auto(group):
        # An auto group is armed on levels derived from its legs, so a basket
        # those cannot be derived from must not be armed at all — it would be
        # monitored with nothing to trigger on.
        _, problem = auto_basis(db, group, live_map)
        if problem:
            return False, f"Can't deploy '{group.name}' on automatic levels — {problem}"
    elif group.stoploss is None and group.target is None:
        return False, f"'{group.name}' needs a stoploss or a target before deploying."
    err = validate_levels(group.stoploss, group.target)
    if err:
        return False, err

    # No live book means nothing can be verified against it, so nothing is
    # rejected on its account — the same stance as a closed leg.
    book = live_map or {}
    problems = [
        p for p in (
            validate_lot_multiple(leg.quantity, leg.tradingsymbol,
                                  (lot_sizes or {}).get(leg.tradingsymbol))
            for leg in legs
            if leg_state(book.get((leg.tradingsymbol, leg.product))) == OPEN
        ) if p
    ]
    if problems:
        return False, (f"Can't deploy '{group.name}' — "
                       + " ".join(problems))
    # Arming starts an auto group's journey: opening levels taken from the legs as
    # they stand at the moment the user commits to them, and the ratchet's anchor
    # back at zero profit. Done after the checks above so a refused deploy has
    # moved nothing.
    refresh_auto_levels(db, group, live_map, commit=False)
    group.status = DEPLOYED
    group.deployed_at = datetime.datetime.utcnow()
    group.trigger_type = None
    group.trigger_message = None
    group.triggered_at = None
    group.triggered_pnl = None
    group.notified_at = None
    set_baseline(group, baseline)
    if is_auto(group):
        record_level_event(
            db, group, ARMED,
            note=(f"Armed on {len(legs)} leg(s) — expected profit "
                  f"₹{group.auto_expected_profit or 0:,.2f}"))
    db.commit()
    return True, None


def set_baseline(group, baseline):
    """Freeze (or clear) the expected range a group was armed against.

    Passing ``None`` clears it, which is what undeploying does: the range was
    the one accepted for *that* arming, and a group returned to draft has no
    live commitment for it to be a reference against.
    """
    group.baseline_spot = baseline and baseline.get('spot')
    group.baseline_sigma = baseline and baseline.get('sigma')
    group.baseline_iv = baseline and baseline.get('iv')
    group.baseline_at = datetime.datetime.utcnow() if baseline else None


def has_baseline(group):
    """True when a group carries a usable frozen range."""
    return bool(getattr(group, 'baseline_spot', None)
                and getattr(group, 'baseline_sigma', None))


def undeploy(db, group):
    """Return a group to draft, clearing any trigger state.

    An auto group's journey ends here and is recorded as ending: the ratcheted
    stoploss was a commitment to *this* arming, so the next deploy starts a fresh
    one from the legs rather than resuming a stop set against a position that may
    have changed while the group sat in draft. The history is kept — it is what
    happened.
    """
    was_armed = is_auto(group) and group.status in (DEPLOYED, TRIGGERED)
    group.status = DRAFT
    group.trigger_type = None
    group.trigger_message = None
    group.triggered_at = None
    group.triggered_pnl = None
    group.notified_at = None
    set_baseline(group, None)
    if was_armed:
        record_level_event(db, group, DISARMED, pnl=group.last_pnl,
                           note="Undeployed — returned to draft")
    db.commit()
    # Back in draft, so the opening levels are re-derived and shown as what the
    # group would arm at next.
    refresh_auto_levels(db, group)
