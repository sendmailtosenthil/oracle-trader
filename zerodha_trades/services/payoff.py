"""Payoff-diagram maths for a trade group.

Answers one question: if the underlying were at price *S*, what would this
group's P&L be? Two curves come out of it —

    at expiry   every front-month contract settled at its intrinsic value
    today       every contract repriced by Black-Scholes at today's clock

— plus the ±1SD / ±2SD bands that say how far the underlying plausibly travels
before the front expiry.

The group's *total* P&L is what the dashboard card shows, so the curves are
anchored to it rather than to the open legs alone: everything that cannot move
with the underlying (settled P&L, legs on another underlying, contracts that
could not be resolved) is folded into a constant ``offset``. That makes the
curve pass exactly through the card's figure at the current spot, which is the
property that stops the chart and the table disagreeing.

Nothing here calls the broker. Every number comes from the positions snapshot
the poller already writes, which is what keeps the chart free on a small host:

* each leg's mark is its ``last_price`` in the snapshot;
* volatility is each option's own implied vol, got by inverting Black-Scholes
  on that mark — so the today-curve reprices the position with the vols the
  market is actually charging, and the SD bands come from the near-the-money
  contract the group holds;
* the underlying's price is derived from the same marks (see
  :func:`spot_from_book`), because Kite's quote endpoints are gated behind a
  Kite Connect subscription and an enctoken session cannot reach them.

Spot is optional. The at-expiry payoff is intrinsic value against the entry
price and needs no underlying price at all, so a group that cannot yield one
still gets its payoff diagram — just without the today-curve, the spot marker
and the SD bands, all three of which genuinely depend on knowing where the
underlying is.

Rates are ignored (``r = 0``): the horizons here are days to a couple of months
and the carry is far smaller than the bid-ask on the options being marked.

Pure functions — no Streamlit, no database, no broker calls. Feeding it needs
the per-leg dicts :func:`~zerodha_trades.services.groups.mark_group` produces
and the instruments-dump rows for their symbols.
"""
import datetime
import math

from zerodha_trades.services.groups import OPEN

CALL, PUT, FUTURE, EQUITY = 'CE', 'PE', 'FUT', 'EQ'
OPTION_KINDS = (CALL, PUT)
PRICEABLE = (CALL, PUT, FUTURE, EQUITY)

# Indian derivatives stop trading at 15:30 IST, which is 10:00 UTC. Timestamps
# in this project are naive UTC (see groups/positions), so the expiry moment is
# built in UTC to match.
EXPIRY_UTC_TIME = datetime.time(10, 0)
YEAR_SECONDS = 365.0 * 24 * 3600


# ----- contract detail ---------------------------------------------------
def expiry_of(contract):
    """A contract's expiry as a ``date``, or ``None`` (equity has no expiry)."""
    raw = (contract or {}).get('expiry') or ''
    try:
        return datetime.datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def expiry_close(expiry_date):
    """The UTC moment a contract stops trading — 15:30 IST on its expiry day."""
    return datetime.datetime.combine(expiry_date, EXPIRY_UTC_TIME)


def years_to(expiry_date, at):
    """Years from ``at`` until expiry, floored at zero once it has passed."""
    if expiry_date is None:
        return 0.0
    remaining = (expiry_close(expiry_date) - at).total_seconds()
    return max(remaining, 0.0) / YEAR_SECONDS


def spot_from_book(items, contracts, name):
    """``(spot, source)`` for an underlying, out of the positions snapshot alone.

    Kite's ``/oms/quote`` endpoints need a Kite Connect subscription and reject
    an enctoken session, so the underlying's price is *derived* from marks the
    poller has already fetched rather than asked for separately. Two ways, best
    first:

    1. **Put-call parity.** With ``r = 0``, ``C - P = S - K``, so
       ``S = C - P + K`` for a call and put sharing a strike and expiry. Exact
       for European index options. Of the available pairs the most
       at-the-money is used — ``C - P`` is smallest there, and that is also
       where the two marks are tightest.
    2. **A future's mark**, which tracks the underlying to within its basis.

    ``(None, None)`` when the group holds neither: a lone option cannot say
    where its underlying is, and guessing would put the whole chart in the
    wrong place. The at-expiry payoff does not need it.
    """
    pairs, future = {}, None
    for item in items:
        contract = contracts.get(item['leg'].tradingsymbol) or {}
        kind = contract.get('instrument_type')
        if contract.get('name') != name or item['state'] != OPEN or not item['last_price']:
            continue
        if kind in OPTION_KINDS:
            key = (contract.get('expiry'), contract.get('strike'))
            pairs.setdefault(key, {})[kind] = item['last_price']
        elif kind == FUTURE and future is None:
            future = (item['last_price'], item['leg'].tradingsymbol)

    complete = [(abs(sides[CALL] - sides[PUT]), strike, sides)
                for (_, strike), sides in pairs.items()
                if CALL in sides and PUT in sides and strike]
    if complete:
        _, strike, sides = min(complete)
        return sides[CALL] - sides[PUT] + strike, f"put-call parity at {strike:,.0f}"
    if future:
        return future[0], f"{future[1]} mark (includes basis)"
    return None, None


def underlyings(items, contracts):
    """Distinct underlying names among the legs, in first-seen order.

    A group is normally one underlying; a chart needs exactly one, because two
    underlyings have no shared price axis.
    """
    seen = []
    for item in items:
        name = (contracts.get(item['leg'].tradingsymbol) or {}).get('name')
        if name and name not in seen:
            seen.append(name)
    return seen


# ----- Black-Scholes -----------------------------------------------------
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def intrinsic(kind, spot, strike):
    """What a contract is worth with no time left."""
    if kind == CALL:
        return max(spot - strike, 0.0)
    if kind == PUT:
        return max(strike - spot, 0.0)
    return spot                    # a future/equity simply is the underlying


def bs_price(kind, spot, strike, t_years, sigma):
    """Black-Scholes price of a European option with ``r = 0``.

    Degrades to :func:`intrinsic` once there is no time or no volatility left,
    which is exactly the right limit and keeps the caller from special-casing
    expiry day.
    """
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return intrinsic(kind, spot, strike)
    v = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + 0.5 * v * v) / v
    d2 = d1 - v
    if kind == CALL:
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


# Bisection beats Newton here: no derivative to code, no divergence on a
# deep-in-the-money mark where vega is ~0, and 60 halvings of [0.5%, 500%] pin
# the vol to well under a basis point. Cost is 60 evaluations of a closed form.
IV_LOW, IV_HIGH, IV_STEPS = 0.005, 5.0, 60


def implied_vol(kind, spot, strike, t_years, market_price):
    """Volatility that reprices ``market_price``, or ``None`` if there isn't one.

    ``None`` is returned rather than a guess whenever the quote carries no time
    value to invert — an expired contract, a mark at or below intrinsic (which
    happens on a stale or wide-spread option), or one so rich that no vol in
    range reaches it. Callers fall back to intrinsic pricing for those legs, so
    a single unusable quote costs the curve its smoothness on one leg instead of
    inventing a volatility.
    """
    if t_years <= 0 or not market_price or market_price <= 0:
        return None
    if market_price <= intrinsic(kind, spot, strike) + 1e-6:
        return None
    if bs_price(kind, spot, strike, t_years, IV_HIGH) < market_price:
        return None
    low, high = IV_LOW, IV_HIGH
    for _ in range(IV_STEPS):
        mid = 0.5 * (low + high)
        if bs_price(kind, spot, strike, t_years, mid) < market_price:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


# ----- the diagram -------------------------------------------------------
def leg_price(leg, spot_at, at):
    """Theoretical price of one leg's contract at underlying ``spot_at``, as of ``at``."""
    kind = leg['kind']
    if kind == EQUITY:
        return spot_at
    if kind == FUTURE:
        # The future tracks the underlying with a basis that converges to zero
        # at its own expiry; decay it linearly rather than pretend a future is
        # the spot today or that it keeps its premium to the end.
        t_now, t_at = leg['t_now'], years_to(leg['expiry'], at)
        left = (t_at / t_now) if t_now > 0 else 0.0
        return spot_at + leg['basis'] * left
    t = years_to(leg['expiry'], at)
    if t <= 0 or leg['iv'] is None:
        return intrinsic(kind, spot_at, leg['strike'])
    return bs_price(kind, spot_at, leg['strike'], t, leg['iv'])


def pnl_at(priced, offset, spot_at, at):
    """The group's P&L with the underlying at ``spot_at``, valued as of ``at``."""
    return offset + sum(
        leg['quantity'] * (leg_price(leg, spot_at, at) - leg['avg'])
        for leg in priced
    )


def build(items, contracts, spot=None, underlying=None, now=None, points=161,
          half_width=None):
    """Payoff curves for a group. Returns ``None`` when nothing can be priced.

    ``items`` are :func:`~zerodha_trades.services.groups.mark_group`'s per-leg
    dicts and ``contracts`` the instruments-dump rows keyed by tradingsymbol.
    ``underlying`` restricts the curve to one name in a group that spans
    several; the rest are folded into the flat offset so the total still
    reconciles.

    ``spot`` may be ``None`` — see the module docstring. Without it the returned
    ``now_pnl`` and ``sigma`` are ``None`` and the chart is centred on the
    strikes instead; the at-expiry curve is unaffected either way.

    ``half_width`` overrides the plotted price range (in points either side of
    centre); by default it is wide enough to hold every strike and three
    standard deviations.
    """
    now = now or datetime.datetime.utcnow()
    if spot is not None and spot <= 0:
        spot = None

    priced, offset, unresolved = [], 0.0, []
    for item in items:
        # Settled P&L is banked history — it does not move with the underlying,
        # whatever the leg is doing now.
        offset += item['settled']
        contract = contracts.get(item['leg'].tradingsymbol)
        kind = (contract or {}).get('instrument_type')
        expiry = expiry_of(contract)
        wrong_underlying = underlying is not None and (contract or {}).get('name') != underlying
        if item['state'] != OPEN or kind not in PRICEABLE or wrong_underlying \
                or (expiry is None and kind != EQUITY):
            offset += item['open_pnl']
            if item['state'] == OPEN and not wrong_underlying and contract is None:
                unresolved.append(item['leg'].tradingsymbol)
            continue
        priced.append(_priced_leg(item, contract, kind, expiry, spot, now))

    if not priced:
        return None

    front = min((leg['expiry'] for leg in priced if leg['expiry']), default=None)
    t_front = years_to(front, now)
    sigma, atm_iv = sd_move(priced, spot, front, t_front) if spot else (None, None)
    centre = spot or strike_centre(priced)
    if half_width is None:
        half_width = default_width(centre, priced, sigma)

    step = (2.0 * half_width) / (points - 1)
    prices = [centre - half_width + step * i for i in range(points)]
    settle_at = expiry_close(front) if front else now
    expiry_pnl = [pnl_at(priced, offset, x, settle_at) for x in prices]

    return {
        'spot': spot,
        'centre': centre,
        'prices': prices,
        'expiry_pnl': expiry_pnl,
        # Both need to know where the underlying is; without that the position
        # cannot be repriced at today's clock, only settled at expiry.
        'now_pnl': [pnl_at(priced, offset, x, now) for x in prices] if spot else None,
        'current_pnl': pnl_at(priced, offset, spot, now) if spot else None,
        'offset': offset,
        'priced': priced,
        'unresolved': unresolved,
        'front_expiry': front,
        'sigma': sigma,
        'atm_iv': atm_iv,
        'half_width': half_width,
        'breakevens': breakevens(prices, expiry_pnl),
        # Several expiries means the "at expiry" curve settles the front month
        # and still carries time value on the rest — worth saying out loud.
        'multi_expiry': len({leg['expiry'] for leg in priced if leg['expiry']}) > 1,
    }


def _priced_leg(item, contract, kind, expiry, spot, now):
    """One open leg, reduced to what :func:`leg_price` needs.

    With no ``spot`` there is nothing to imply a vol against, so the leg carries
    none and prices at intrinsic — which is exactly right for the at-expiry
    curve, the only one drawn in that case.
    """
    leg, ltp = item['leg'], item['last_price'] or 0.0
    strike = float(contract.get('strike') or 0.0)
    t_now = years_to(expiry, now)
    return {
        'symbol': leg.tradingsymbol,
        'kind': kind,
        'strike': strike,
        'expiry': expiry,
        't_now': t_now,
        'quantity': leg.quantity,
        'avg': item['average_price'] or 0.0,
        'ltp': ltp,
        'iv': (implied_vol(kind, spot, strike, t_now, ltp)
               if spot and kind in OPTION_KINDS else None),
        # Futures trade at a premium/discount to spot; keep today's gap so the
        # curve starts from the price the book is actually marked at.
        'basis': (ltp - spot) if (spot and kind == FUTURE) else 0.0,
    }


def sd_move(priced, spot, front, t_front):
    """``(sigma, iv)`` — one SD of underlying travel by the front expiry.

    Uses the implied vol of the held option nearest the money — the contract
    whose quote the market prices most confidently — rather than a volatility
    index, so the bands describe *this* position's expiry. ``(None, None)`` when
    the group holds no option with a usable quote.

    The vol is returned alongside because it is the half of this worth
    *storing*: a group freezes it at deploy so later drift can be read as a vol
    move rather than as the time decay that dominates sigma.
    """
    if t_front <= 0:
        return None, None
    options = [leg for leg in priced if leg['kind'] in OPTION_KINDS and leg['iv']]
    if not options:
        return None, None
    front_month = [leg for leg in options if leg['expiry'] == front] or options
    nearest = min(front_month, key=lambda leg: abs(leg['strike'] - spot))
    return spot * nearest['iv'] * math.sqrt(t_front), nearest['iv']


def strike_centre(priced):
    """Where to centre the axis when spot is unknown — the middle of the strikes.

    Falls back to a leg's own mark for a futures-only group, which has no
    strikes at all.
    """
    strikes = [leg['strike'] for leg in priced if leg['strike']]
    if strikes:
        return (min(strikes) + max(strikes)) / 2.0
    return next((leg['ltp'] for leg in priced if leg['ltp']), 0.0)


def default_width(centre, priced, sigma):
    """Half-width of the price axis: every strike, and three SDs, comfortably in."""
    strikes = [leg['strike'] for leg in priced if leg['strike']]
    reach = max((abs(k - centre) for k in strikes), default=0.0)
    return max(3.0 * (sigma or 0.0), 1.4 * reach, 0.04 * centre) or 1.0


def breakevens(prices, curve):
    """Underlying prices where the expiry curve crosses zero.

    Linear interpolation between grid points, which is exact away from the
    strikes because the expiry payoff is piecewise linear.
    """
    out = []
    for i in range(1, len(curve)):
        a, b = curve[i - 1], curve[i]
        if a == 0.0:
            out.append(prices[i - 1])
        elif (a < 0) != (b < 0):
            out.append(prices[i - 1] + (prices[i] - prices[i - 1]) * (-a / (b - a)))
    return out
