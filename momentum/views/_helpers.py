"""Shared data access for the momentum views — nothing cached in RAM.

The ranking is computed on refresh / settings-change and persisted to the
``momentum_rankings`` DB table; the views read it from there (``get_ranking``).
Prices are never held in memory across requests: build a small close-only
``PriceBook`` for just the symbols a view needs (holdings, or the rebalance
pool) via ``price_book(symbols)``. The full universe is touched only transiently
during a refresh (streamed, then freed).
"""
from collections import Counter

import streamlit as st

from momentum.services import data as mdata
from momentum.services import strategy


def price_book(symbols):
    """Close-only PriceBook for a SMALL symbol set (e.g. holdings + pool), built
    by streaming just those files. Never build the full universe here."""
    return mdata.PriceBook.from_cache(symbols=list(symbols) if symbols else [])


def get_ranking(db):
    """Latest persisted ranking, read from the DB (no prices in RAM)."""
    return strategy.read_ranking(db)


def rank_map(ranking):
    return {r["symbol"]: r["rank"] for r in ranking["ranked"]}


def raw_rank_map(ranking):
    return {r["symbol"]: r.get("raw_rank") for r in ranking["ranked"]}


def exclusion_summary(ranking, top=4):
    """Short 'why nothing ranked' breakdown (only populated right after a compute)."""
    exc = ranking.get("excluded") or []
    if not exc:
        return ""
    counts = Counter(e["reason"] for e in exc)
    return ", ".join(f"{n} {reason}" for reason, n in counts.most_common(top))


def auto_refresh_constituents():
    """Self-heal the universe file on load (no network unless missing/stale)."""
    return mdata.ensure_current_constituents()


def render_no_ranking(ranking):
    """Explain why nothing ranked and point to the fix (usually: build history)."""
    st.warning("No ranked stocks for the latest date.")
    summary = exclusion_summary(ranking)
    if summary:
        st.caption(f"Universe of {ranking.get('n_universe', 0)} excluded — {summary}.")
    st.info("If the ranking is empty, the current Nifty 500 names likely don't yet have "
            "the ~1-year of daily prices the 3/6/9-month lookback needs. Go to "
            "**Refresh prices → Build / repair full price history (one-time)** and fetch "
            "from ~15 months back — that computes and stores the ranking.")
