"""Shared, cached data access for the momentum views.

Scoring the whole Nifty500 universe means loading ~95 MB of cached daily bars
and building a PriceBook over ~650 symbols, so we cache aggressively:

- the PriceBook + Calendar bundle is a ``st.cache_resource`` singleton, and
- the scored ranking is ``st.cache_data`` keyed on the cache signature + the
  scoring config.

Both invalidate automatically when the on-disk cache changes (a price refresh
rewrites files, bumping the max mtime) or the config changes.
"""
import os
import types
from collections import Counter

import streamlit as st

from momentum.services import data as mdata
from momentum.services import strategy


def cache_sig():
    """Cheap signature of the price cache: (max mtime, file count)."""
    latest, n = 0.0, 0
    try:
        with os.scandir(mdata.cache_dir()) as it:
            for e in it:
                if e.name.endswith(".json"):
                    n += 1
                    latest = max(latest, e.stat().st_mtime)
    except OSError:
        pass
    return (round(latest, 3), n)


@st.cache_resource(show_spinner=False)
def _bundle(sig):
    series = mdata.load_series()
    return mdata.PriceBook(series), mdata.Calendar.from_series(series)


def price_book():
    return _bundle(cache_sig())[0]


@st.cache_data(show_spinner="Scoring Nifty500 momentum…")
def _ranking(sig, constituents_date, factors_json, vol_enabled, vol_months, min_cov):
    pb, cal = _bundle(sig)
    cfg = types.SimpleNamespace(factors_json=factors_json, vol_enabled=vol_enabled,
                                vol_months=vol_months, min_history_coverage=min_cov)
    return strategy.rank_universe(pb, cal, cfg)


def get_ranking(db):
    """Cached momentum ranking for the latest cached trading day."""
    cfg = strategy.get_config(db)
    cons_date = mdata.universe_status().get("snapshot_date")
    return _ranking(cache_sig(), cons_date, cfg.factors_json, bool(cfg.vol_enabled),
                    cfg.vol_months, cfg.min_history_coverage)


def rank_map(ranking):
    return {r["symbol"]: r["rank"] for r in ranking["ranked"]}


def raw_rank_map(ranking):
    return {r["symbol"]: r.get("raw_rank") for r in ranking["ranked"]}


def exclusion_summary(ranking, top=4):
    """Short 'why nothing ranked' breakdown, e.g. '498 insufficient-history, 2 no-price-data'."""
    exc = ranking.get("excluded") or []
    if not exc:
        return ""
    counts = Counter(e["reason"] for e in exc)
    return ", ".join(f"{n} {reason}" for reason, n in counts.most_common(top))


@st.cache_data(show_spinner="Checking Nifty 500 constituents…")
def _ensure_constituents(reconstitution_iso):
    # Keyed on the current reconstitution date so it runs once per period per
    # process; auto-downloads the official list only when missing/stale.
    return mdata.ensure_current_constituents()


def auto_refresh_constituents():
    """Self-heal the universe file on load. Returns the ensure-result dict.
    Cheap in the normal case (no network unless the file is missing/stale)."""
    return _ensure_constituents(mdata.latest_reconstitution().isoformat())


def render_no_ranking(ranking):
    """Explain why nothing ranked and point to the fix (usually: build history)."""
    st.warning("No ranked stocks for the latest date.")
    summary = exclusion_summary(ranking)
    if summary:
        st.caption(f"Universe of {ranking.get('n_universe', 0)} excluded — {summary}.")
    st.info("If most are **insufficient-history**, the current Nifty 500 names don't yet "
            "have the ~1-year of daily prices the 3/6/9-month lookback needs. Go to "
            "**Refresh prices → Build / repair price history (one-time)** and fetch from "
            "~15 months back. (The 'History from' metric reflects the *oldest* cached "
            "symbol, not every current name — so it can look complete when it isn't.)")


def clear_caches():
    """Drop the cached ranking + price-book bundle (call after a price refresh
    or config change). These are the actual cached callables — the public
    ``get_ranking``/``price_book`` wrappers are not decorated."""
    _ranking.clear()
    _bundle.clear()
