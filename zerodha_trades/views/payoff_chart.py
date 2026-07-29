"""The payoff diagram behind a group card's 📈 view.

Draws two curves against the underlying's price — what the group is worth at the
front expiry, and what it is worth right now — over the ±1SD / ±2SD bands the
held options imply. The group's stoploss and target ride along as horizontal
levels, so the chart answers the question the rest of this module exists for:
*where does the underlying have to go for this to trip?*

The maths is all in :mod:`zerodha_trades.services.payoff`; this file is purely
the picture. Plotly is already a dependency (the Bees dashboard uses it) and the
figure is ~160 points across two traces, so nothing here costs the host anything
it was not already paying.
"""
import datetime
import math

import plotly.graph_objects as go
import streamlit as st

from zerodha_trades.services import payoff as PO
from zerodha_trades.views import _helpers as H

# Slots 1 and 2 of the categorical palette, stepped per mode, plus the chrome
# and status inks. Streamlit's own plotly theme is switched off (`theme=None`)
# so these are what actually render; backgrounds stay transparent to inherit the
# dialog surface.
LIGHT = {
    'expiry': '#2a78d6', 'now': '#eb6834',
    'text': '#52514e', 'muted': '#898781',
    'grid': '#e1e0d9', 'axis': '#c3c2b7',
    'band': 'rgba(137,135,129,0.10)', 'band_outer': 'rgba(137,135,129,0.05)',
}
DARK = {
    'expiry': '#3987e5', 'now': '#d95926',
    'text': '#c3c2b7', 'muted': '#898781',
    'grid': '#2c2c2a', 'axis': '#383835',
    'band': 'rgba(195,194,183,0.09)', 'band_outer': 'rgba(195,194,183,0.045)',
}
GOOD, CRITICAL = '#0ca30c', '#d03b3b'   # status inks — target and stoploss

CHART_HEIGHT = 430
GRID_POINTS = 161


def _palette():
    """Colours for the viewer's current theme."""
    try:
        base = st.context.theme.type
    except Exception:  # noqa: BLE001 - older Streamlit, or no browser context
        base = st.get_option("theme.base")
    return DARK if base == 'dark' else LIGHT


def render(db, mark):
    """The payoff view for one group. Falls back to a message when unplottable."""
    group, items = mark['group'], mark['legs']
    contracts = H.contracts(db, [item['leg'].tradingsymbol for item in items])
    if not contracts:
        st.warning(
            "Couldn't reach the instruments master, so these contracts can't be "
            "priced. The **Table** view still works — try the chart again in a "
            "moment."
        )
        return

    names = PO.underlyings(items, contracts)
    if not names:
        st.info("None of this group's legs resolve to a tradable contract, so "
                "there is nothing to plot.")
        return
    name = names[0]
    if len(names) > 1:
        # Two underlyings have no shared price axis. Plot one and fold the rest
        # into the flat offset, so the curve still passes through the group's
        # real P&L at spot.
        name = st.selectbox("Underlying", names, key=f"ztrade_payoff_und_{group.id}")

    spot, source = PO.spot_from_book(items, contracts, name)

    probe = PO.build(items, contracts, spot, underlying=name, points=2)
    if probe is None:
        st.info(
            "Every leg on this underlying is closed, so the group's P&L is "
            "settled and no longer moves with the price — there is no payoff "
            "left to draw. See the **Table** view for the breakdown."
        )
        return

    centre = probe['centre']
    auto_pct = max(1, min(40, round(probe['half_width'] / centre * 100)))
    pct = st.slider("Price range (± %)", 1, 40, auto_pct, key=f"ztrade_payoff_zoom_{group.id}",
                    help="How far either side of centre to plot. Widen it to see "
                         "where the wings finish; narrow it to read the middle.")
    data = PO.build(items, contracts, spot, underlying=name,
                    points=GRID_POINTS, half_width=centre * pct / 100.0)

    st.plotly_chart(_figure(group, data, name),
                    use_container_width=True, theme=None,
                    config={'displaylogo': False,
                            'modeBarButtonsToRemove': ['select2d', 'lasso2d',
                                                       'autoScale2d']},
                    key=f"ztrade_payoff_fig_{group.id}")
    _summary(data, source, len(names))


def _figure(group, data, name):
    colour = _palette()
    prices, spot = data['prices'], data['spot']
    front = data['front_expiry']
    expiry_label = f"At expiry ({front:%d %b})" if front else "At settlement"

    fig = go.Figure()
    _sd_bands(fig, data, colour)
    fig.add_hline(y=0, line=dict(color=colour['axis'], width=1))

    # Unified hover names each series off its `name`, so the template carries
    # only the amount and the trailing box is suppressed.
    amount = '₹%{y:,.0f}<extra></extra>'
    fig.add_trace(go.Scatter(
        x=prices, y=data['expiry_pnl'], name=expiry_label, mode='lines',
        line=dict(color=colour['expiry'], width=2), hovertemplate=amount,
    ))
    if data['now_pnl'] is not None:
        fig.add_trace(go.Scatter(
            x=prices, y=data['now_pnl'], name='Today', mode='lines',
            line=dict(color=colour['now'], width=2), hovertemplate=amount,
        ))
    if data['breakevens']:
        fig.add_trace(go.Scatter(
            x=data['breakevens'], y=[0] * len(data['breakevens']),
            mode='markers', name='Breakeven', showlegend=False,
            marker=dict(size=9, color=colour['text'], symbol='circle-open',
                        line=dict(width=2)),
            hovertemplate='Breakeven %{x:,.0f}<extra></extra>',
        ))

    lo, hi = _y_range(group, data)
    _levels(fig, group, (lo, hi), colour)
    if spot:
        fig.add_vline(x=spot, line=dict(color=colour['text'], width=1.5),
                      annotation_text=f"Spot {spot:,.2f}",
                      annotation_position="top",
                      annotation_font=dict(size=11, color=colour['text']))

    ticks = _nice_ticks(lo, hi)
    fig.update_layout(
        height=CHART_HEIGHT,
        # Room for the rupee ticks on the left and the price ticks plus axis
        # title underneath; the top strip holds the legend and the SD labels.
        margin=dict(l=70, r=16, t=64, b=48),
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        hovermode='x unified',
        # The dialog redraws every few seconds. A stable uirevision tells Plotly
        # to keep whatever the user zoomed or panned to instead of snapping back
        # to the full range on every tick.
        uirevision=f"ztrade-payoff-{group.id}",
        font=dict(color=colour['text'], size=12),
        legend=dict(orientation='h', yanchor='bottom', y=1.10,
                    xanchor='left', x=0, bgcolor='rgba(0,0,0,0)'),
    )
    fig.update_xaxes(
        title_text=f"{name} price", title_standoff=8,
        title_font=dict(size=11, color=colour['muted']),
        # Pinned to the data. Left to itself Plotly pads the axis out to whatever
        # reference line sits furthest away — a 2SD marker outside the zoom, say —
        # which leaves the curves looking clipped short of the plot edge.
        range=[prices[0], prices[-1]],
        showgrid=False, zeroline=False,
        linecolor=colour['axis'], tickfont=dict(size=11, color=colour['muted']),
        tickformat=',.0f',
    )
    fig.update_yaxes(
        title_text="Profit / loss", title_standoff=8,
        title_font=dict(size=11, color=colour['muted']),
        range=[lo, hi], tickvals=ticks,
        ticktext=[_short_money(t) for t in ticks],
        gridcolor=colour['grid'], zeroline=False,
        tickfont=dict(size=11, color=colour['muted']),
    )
    return fig


def _sd_bands(fig, data, colour):
    """Shade ±1SD and ±2SD, and label the four edges.

    The inner band is one rect straddling spot; the outer is the two shoulders
    either side of it, so the shading darkens toward the middle rather than
    double-painting it.
    """
    sigma, spot = data['sigma'], data['spot']
    if not sigma:
        return
    bands = [
        (spot - 2 * sigma, spot - sigma, colour['band_outer']),
        (spot + sigma, spot + 2 * sigma, colour['band_outer']),
        (spot - sigma, spot + sigma, colour['band']),
    ]
    for x0, x1, fill in bands:
        fig.add_vrect(x0=x0, x1=x1, fillcolor=fill, line_width=0, layer='below')
    for multiple in (1, 2):
        for sign in (-1, 1):
            fig.add_vline(x=spot + sign * multiple * sigma,
                          line=dict(color=colour['grid'], width=1),
                          annotation_text=f"{'-' if sign < 0 else ''}{multiple}SD",
                          annotation_position="top",
                          annotation_font=dict(size=10, color=colour['muted']))


def _levels(fig, group, bounds, colour):
    """The group's stoploss and target as horizontal levels, where they fit."""
    lo, hi = bounds
    for value, ink, label in ((group.stoploss, CRITICAL, "🛑 Stoploss"),
                              (group.target, GOOD, "🎯 Target")):
        if value is None or not lo <= value <= hi:
            continue
        # Labelled inside the plot, right-hand end: parking them outside would
        # cost a margin wide enough to hold "🛑 Stoploss -₹1.50L", and the left
        # end is where the rupee ticks are.
        fig.add_hline(y=value, line=dict(color=ink, width=1.5, dash='dash'),
                      annotation_text=f"{label} {_short_money(value)}",
                      annotation_position="top right",
                      annotation_font=dict(size=11, color=colour['text']))


def _y_range(group, data):
    """P&L axis bounds: the curves, plus any level close enough to be worth seeing."""
    values = list(data['expiry_pnl']) + list(data['now_pnl'] or []) + [0.0]
    lo, hi = min(values), max(values)
    span = (hi - lo) or max(abs(hi), 1.0)
    for level in (group.stoploss, group.target):
        # A level an order of magnitude outside the curve would flatten it to a
        # line, so it is left off the axis rather than allowed to set the scale.
        if level is not None and lo - span <= level <= hi + span:
            lo, hi = min(lo, level), max(hi, level)
    pad = (hi - lo) * 0.10 or 1.0
    return lo - pad, hi + pad


def _summary(data, source, n_underlyings):
    """The numbers the curve implies, written out under it."""
    sigma, spot = data['sigma'], data['spot']
    best, worst = max(data['expiry_pnl']), min(data['expiry_pnl'])
    left, middle, right = st.columns(3)
    left.markdown(f"**Now** {H.colored_money(data['current_pnl'])}")
    middle.markdown(f"**Best at expiry** {H.colored_money(best)}")
    right.markdown(f"**Worst at expiry** {H.colored_money(worst)}")

    notes = []
    if data['breakevens']:
        notes.append("Breakeven at " + " and ".join(f"**{b:,.0f}**"
                                                    for b in data['breakevens']))
    if sigma:
        days = max((PO.expiry_close(data['front_expiry'])
                    - datetime.datetime.utcnow()).total_seconds() / 86400.0, 0.0)
        notes.append(f"1SD ≈ **±{sigma:,.0f}** ({spot - sigma:,.0f}–{spot + sigma:,.0f}) "
                     f"over {days:.1f} day(s) to expiry")
    if source:
        notes.append(f"underlying **{spot:,.2f}** by {source}")
    notes.append(f"redrawn every {H.LIVE_SECONDS}s")
    st.caption(" · ".join(notes) + ".")

    caveats = []
    if not spot:
        caveats.append(
            "This group holds no call/put pair on a shared strike and no future, "
            "so the underlying's price can't be derived from the book — and Kite's "
            "quote feed needs a Kite Connect subscription an enctoken can't use. "
            "That leaves the **at expiry** payoff, which doesn't depend on it; the "
            "today-curve, spot marker and SD bands are the three things that do.")
    if data['unresolved']:
        caveats.append(
            "Couldn't resolve " + ", ".join(f"**{s}**" for s in data['unresolved'][:3])
            + " in the instruments master; their P&L is held flat in the curve "
              "rather than repriced.")
    if data['multi_expiry']:
        caveats.append(
            "Legs expire on different dates. The **at expiry** curve settles the "
            "front month and still carries Black-Scholes time value on the rest, "
            "so it is a T+0-at-front-expiry line, not a final payoff.")
    if n_underlyings > 1:
        caveats.append(
            "This group spans several underlyings. Legs on the other ones are "
            "held flat, so the curve still meets the group's true P&L at spot but "
            "only moves with the underlying selected above.")
    if caveats:
        st.caption("ℹ️ " + "  ".join(caveats))


# ----- rupee axis --------------------------------------------------------
def _short_money(value):
    """``₹1.5L`` / ``-₹3Cr`` — Indian short scale, for axis ticks."""
    magnitude = abs(value)
    if magnitude < 0.5:
        return "₹0"
    if magnitude >= 1e7:
        text = f"{magnitude / 1e7:,.2f}Cr".replace(".00Cr", "Cr")
    elif magnitude >= 1e5:
        text = f"{magnitude / 1e5:,.2f}L".replace(".00L", "L")
    elif magnitude >= 1e3:
        text = f"{magnitude / 1e3:,.1f}k".replace(".0k", "k")
    else:
        text = f"{magnitude:,.0f}"
    return f"{'-' if value < 0 else ''}₹{text}"


NICE_STEPS = (1, 2, 2.5, 5, 10)


def _nice_ticks(lo, hi, target=6):
    """Round tick positions across ``[lo, hi]``, roughly ``target`` of them."""
    span = hi - lo
    if span <= 0:
        return [lo]
    magnitude = 10 ** math.floor(math.log10(span / target))
    step = next(m * magnitude for m in NICE_STEPS if m * magnitude >= span / target)
    ticks, value = [], math.ceil(lo / step) * step
    while value <= hi + step * 1e-6:
        ticks.append(value)
        value += step
    return ticks or [lo, hi]
