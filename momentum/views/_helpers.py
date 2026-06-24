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
    as_of = cal.last()
    if as_of is None:
        return {"as_of": None, "ranked": [], "excluded": [], "snapshot_date": None, "n_universe": 0}
    member = mdata.Universe.load().as_of(as_of)
    candidates = [mdata.to_yahoo(s) for s in member["symbols"]]
    if not candidates:
        candidates = list(pb._by_date.keys())
    cfg = types.SimpleNamespace(factors_json=factors_json, vol_enabled=vol_enabled,
                                vol_months=vol_months, min_history_coverage=min_cov)
    ranked, excluded = strategy.score_universe(pb, cal, candidates, as_of, cfg)
    return {"as_of": as_of, "ranked": ranked, "excluded": excluded,
            "snapshot_date": member["snapshot_date"], "n_universe": len(candidates)}


def get_ranking(db):
    """Cached momentum ranking for the latest cached trading day."""
    cfg = strategy.get_config(db)
    cons_date = mdata.universe_status().get("snapshot_date")
    return _ranking(cache_sig(), cons_date, cfg.factors_json, bool(cfg.vol_enabled),
                    cfg.vol_months, cfg.min_history_coverage)


def rank_map(ranking):
    return {r["symbol"]: r["rank"] for r in ranking["ranked"]}


@st.cache_data(show_spinner="Checking Nifty 500 constituents…")
def _ensure_constituents(reconstitution_iso):
    # Keyed on the current reconstitution date so it runs once per period per
    # process; auto-downloads the official list only when missing/stale.
    return mdata.ensure_current_constituents()


def auto_refresh_constituents():
    """Self-heal the universe file on load. Returns the ensure-result dict.
    Cheap in the normal case (no network unless the file is missing/stale)."""
    return _ensure_constituents(mdata.latest_reconstitution().isoformat())


def clear_caches():
    """Drop the cached ranking + price-book bundle (call after a price refresh
    or config change). These are the actual cached callables — the public
    ``get_ranking``/``price_book`` wrappers are not decorated."""
    _ranking.clear()
    _bundle.clear()
