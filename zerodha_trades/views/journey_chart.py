"""The stoploss and target timeline of an auto group — every level it has held.

An auto group's levels move on their own: the stoploss opens at the whole premium
the basket was sold for and steps up as profit builds, the target climbs its
ladder as each rung is taken, and both are re-priced when the legs change. Those
moves are the trade's actual risk history, so each one is recorded with its time
and its reason (``ztrade_group_level_events``) and drawn here as it happened — a
staircase, because a level holds until something replaces it.

Three things are on the one time axis: the stoploss, the target, and the P&L that
moved them, with a dotted upright at each target taken. So the chart answers what
the numbers on the card cannot — how long each rung took, how much of the profit
the stop has locked in, and how much room is left before it is taken out. Hover a
step and it says why it moved; the table under it is the same history in figures.

Levels are drawn in IST wall-clock. Palette and inks come from the payoff chart
so the two views of one group look like the same instrument.
"""
import datetime

import plotly.graph_objects as go
import streamlit as st

from zerodha_trades.services import groups as G
from zerodha_trades.views import _helpers as H
from zerodha_trades.views.payoff_chart import CRITICAL, DARK, GOOD, LIGHT

CHART_HEIGHT = 380

# What each recorded kind is called on screen, and the marker it earns.
LABELS = {
    G.ARMED: "Armed",
    G.SL_ADJUSTED: "Stoploss stepped up",
    G.TARGET_REACHED: "Target reached",
    G.BASIS_CHANGED: "Legs changed — re-priced",
    G.MANUAL: "Taken over by hand",
    G.DISARMED: "Disarmed",
}

# The two kinds that end a journey: nothing automatic moves the levels after
# either, so the staircase stops there rather than running on to the present.
CLOSING = (G.DISARMED, G.MANUAL)


def render(db, group, pnl):
    """The journey for one auto group, or a note on why there isn't one yet."""
    events = G.level_events(db, group.id)
    if not events:
        st.info(
            "Nothing recorded yet — the timeline starts when the group is "
            "deployed. Its opening levels are set from the premium of the "
            "trades in it, and every move after that is kept here with the time "
            "it happened and the reason it moved."
        )
        _basis(db, group)
        return

    st.plotly_chart(
        _figure(group, events, pnl),
        use_container_width=True, theme=None,
        config={'displaylogo': False,
                'modeBarButtonsToRemove': ['select2d', 'lasso2d', 'autoScale2d']},
        key=f"ztrade_journey_fig_{group.id}",
    )
    _basis(db, group)
    _table(events)


def _basis(db, group):
    """The figures behind the levels, and the rule they follow."""
    if not G.is_auto(group):
        st.caption("This group is on fixed levels now — the history above is how "
                   "its stoploss got to where you took it over. Nothing moves "
                   "either level automatically any more.")
        return
    expected, problem = G.auto_basis(db, group)
    if problem:
        st.caption(f"⚠️ {problem}")
        return
    st.caption(
        f"Expected profit **₹{expected:,.2f}** — the premium this basket was "
        f"opened for. Stoploss steps up in **₹{G.threshold_of(group):,.2f}** "
        f"minimum jumps and never down; now working toward "
        f"{G.target_label(group)}."
    )


def _reason(event):
    """One line saying what moved and why — the recorded reason, or its kind."""
    label = LABELS.get(event.kind, event.kind)
    return f"{label}: {event.note}" if event.note else label


def _series(group, events, pnl, now):
    """Level history as plottable series, with a gap between separate journeys.

    The last point is *now* rather than the last event: a level holds until
    something replaces it, so the staircase has to run to the present to say
    where the group actually stands. A journey that ended — disarmed, or taken
    over by hand — gets a ``None`` instead, which breaks the line rather than
    implying the app was still moving a level it had let go of.
    """
    times, stops, targets, pnls, reasons = [], [], [], [], []
    for event in events:
        at = H.ist_dt(event.at)
        times.append(at)
        stops.append(event.stoploss)
        targets.append(event.target)
        pnls.append(event.pnl)
        reasons.append(_reason(event))
        if event.kind in CLOSING:
            times.append(at)
            stops.append(None)
            targets.append(None)
            pnls.append(None)
            reasons.append("")
    if events[-1].kind not in CLOSING:
        times.append(now)
        stops.append(group.stoploss)
        targets.append(group.target)
        pnls.append(pnl)
        reasons.append("Where it stands now")
    return times, stops, targets, pnls, reasons


def _figure(group, events, pnl):
    colour = DARK if H.dark_theme() else LIGHT
    now = H.ist_dt(datetime.datetime.utcnow())
    times, stops, targets, pnls, reasons = _series(group, events, pnl, now)

    fig = go.Figure()
    fig.add_hline(y=0, line=dict(color=colour['axis'], width=1))
    # A dotted upright at each target, so the timeline reads as milestones: how
    # long the trade took to reach each rung, and what the stop did in between.
    for event in (e for e in events if e.kind == G.TARGET_REACHED):
        fig.add_vline(x=H.ist_dt(event.at),
                      line=dict(color=GOOD, width=1, dash='dot'))

    # 'hv' holds each level flat until the next one — which is what a level does.
    fig.add_trace(go.Scatter(
        x=times, y=targets, name="Target", mode='lines+markers', line_shape='hv',
        line=dict(color=GOOD, width=2, dash='dot'),
        marker=dict(size=5, color=GOOD), connectgaps=False,
        hovertemplate="Target %{y:,.0f}<extra></extra>",
    ))
    # Markers on every recorded move, each carrying its reason — the answer to
    # "why did the stop jump there?" is on the point itself.
    fig.add_trace(go.Scatter(
        x=times, y=stops, name="Stoploss", mode='lines+markers', line_shape='hv',
        line=dict(color=CRITICAL, width=2.5),
        marker=dict(size=6, color=CRITICAL), connectgaps=False,
        customdata=reasons,
        hovertemplate="Stoploss %{y:,.0f}<br>%{customdata}<extra></extra>",
    ))
    # The P&L that caused each move, so a step can be read against the profit it
    # came out of. Not a continuous P&L history — only the moments something
    # was recorded, which is all that is kept.
    fig.add_trace(go.Scatter(
        x=times, y=pnls, name="P&L at each move", mode='lines+markers',
        line=dict(color=colour['muted'], width=1, dash='dash'),
        marker=dict(size=5, color=colour['muted']), connectgaps=False,
        hovertemplate="P&L %{y:,.0f}<extra></extra>",
    ))

    reached = [e for e in events if e.kind == G.TARGET_REACHED]
    if reached:
        fig.add_trace(go.Scatter(
            x=[H.ist_dt(e.at) for e in reached],
            # At the P&L that crossed it, which is where the rung actually was —
            # `event.target` by then holds the *next* rung, not the one taken.
            y=[e.pnl for e in reached],
            name="Target hit", mode='markers',
            marker=dict(symbol='star', size=14, color=GOOD,
                        line=dict(width=1, color=colour['grid'])),
            customdata=[e.note or "" for e in reached],
            hovertemplate="%{customdata}<extra></extra>",
        ))

    fig.update_layout(
        height=CHART_HEIGHT,
        margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color=colour['text'], size=12),
        hovermode='x unified',
        legend=dict(orientation='h', yanchor='bottom', y=1.0, x=0,
                    bgcolor='rgba(0,0,0,0)'),
        xaxis=dict(title="IST", gridcolor=colour['grid'],
                   linecolor=colour['axis'], zeroline=False),
        yaxis=dict(title="₹", gridcolor=colour['grid'],
                   linecolor=colour['axis'], zeroline=False, tickformat=",.0f"),
    )
    return fig


def _table(events):
    """The same history in figures, newest first — the record, not the picture."""
    with st.expander(f"Recorded moves ({len(events)})", expanded=False):
        st.dataframe(
            [{
                'When': H.ist(event.at),
                'Event': LABELS.get(event.kind, event.kind),
                'Stoploss': event.stoploss,
                'Target': event.target,
                'P&L': event.pnl,
                'Reason': event.note or "",
            } for event in reversed(events)],
            hide_index=True,
            width='stretch',
            column_config={
                'When': st.column_config.TextColumn(
                    "When (IST)", help="Date and time the level moved."),
                'Reason': st.column_config.TextColumn(
                    "Reason", width="large",
                    help="Why it moved — which target was hit, how big the "
                         "trailing step was, or what changed in the legs."),
                'Stoploss': st.column_config.NumberColumn("Stoploss", format="%.2f"),
                'Target': st.column_config.NumberColumn(
                    "Target", format="%.2f",
                    help="Blank once every target has been taken — the trailing "
                         "stoploss is the only exit left."),
                'P&L': st.column_config.NumberColumn(
                    "P&L", format="%.2f",
                    help="What the group's P&L was when the level moved."),
            },
        )
