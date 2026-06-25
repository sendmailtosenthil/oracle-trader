"""Downloader Analytics page: data-coverage dashboard + options-chain analytics.

Two tabs:
  - Coverage: operational view over DownloadStat/DownloadJob (days, sizes, gaps).
  - Options Chain: market analytics computed from a downloaded day's CSVs.
"""
import datetime
import os

import pandas as pd
import streamlit as st

from common.database import DownloadStat, DownloadJob
from downloader.services import core


def render(db):
    st.title("📊 Downloader Analytics")
    tab_cov, tab_opt = st.tabs(["Coverage", "Options Chain"])
    with tab_cov:
        _render_coverage(db)
    with tab_opt:
        _render_options_chain(db)


# --------------------------------------------------------------------------
# Tab 1 — Coverage dashboard
# --------------------------------------------------------------------------
def _render_coverage(db):
    stats = db.query(DownloadStat).order_by(DownloadStat.date.desc()).all()
    if not stats:
        st.info("No downloads recorded yet. Run a download from **Options Download**.")
        return

    df = pd.DataFrame([{
        "date": s.date, "symbol": s.symbol,
        "index": s.index_status, "vix": s.vix_status,
        "futures": s.futures_status, "options": s.options_status,
        "ce_contracts": s.ce_instruments, "pe_contracts": s.pe_instruments,
        "ce_atm": s.ce_rows, "pe_atm": s.pe_rows,
        "size_mb": round(s.file_size_mb, 2), "upload": s.upload_status,
    } for s in stats])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Days covered", df["date"].nunique())
    c2.metric("Symbols", df["symbol"].nunique())
    c3.metric("Total size", f"{df['size_mb'].sum():.1f} MB")
    c4.metric("Latest day", df["date"].max())

    st.subheader("Coverage by day")
    st.dataframe(df, use_container_width=True, hide_index=True)

    # Missing trading days (weekdays in the covered range with no row per symbol).
    st.subheader("Gaps (missing trading days)")
    any_gap = False
    for symbol in sorted(df["symbol"].unique()):
        have = set(df[df["symbol"] == symbol]["date"])
        lo = datetime.date.fromisoformat(min(have))
        hi = datetime.date.fromisoformat(max(have))
        missing = [d.isoformat() for d in core._trading_days(lo, hi)
                   if d.isoformat() not in have]
        if missing:
            any_gap = True
            st.write(f"**{symbol}**: {len(missing)} missing — {', '.join(missing)}")
    if not any_gap:
        st.success("No gaps — every trading day in range is covered for each symbol.")

    st.subheader("Downloaded size over time")
    size_by_day = df.groupby("date")["size_mb"].sum().sort_index()
    st.bar_chart(size_by_day)

    jobs = db.query(DownloadJob).order_by(DownloadJob.created_at.desc()).limit(15).all()
    if jobs:
        st.subheader("Recent jobs")
        st.dataframe(pd.DataFrame([{
            "created": j.created_at.strftime("%Y-%m-%d %H:%M") if j.created_at else "",
            "type": j.job_type, "range": f"{j.start_date}..{j.end_date}",
            "symbols": j.symbols, "status": j.status, "message": j.message,
        } for j in jobs]), use_container_width=True, hide_index=True)


# --------------------------------------------------------------------------
# Tab 2 — Options-chain analytics
# --------------------------------------------------------------------------
def _render_options_chain(db):
    # Build (symbol -> available dates) from stats where options completed.
    rows = (
        db.query(DownloadStat.symbol, DownloadStat.date)
        .filter(DownloadStat.options_status == "completed")
        .order_by(DownloadStat.date.desc())
        .all()
    )
    by_symbol = {}
    for symbol, date in rows:
        by_symbol.setdefault(symbol, []).append(date)
    if not by_symbol:
        st.info("No completed options downloads yet.")
        return

    c1, c2 = st.columns(2)
    symbol = c1.selectbox("Symbol", sorted(by_symbol.keys()))
    date_str = c2.selectbox("Date", by_symbol[symbol])

    opt_path = core.data_file_path(symbol, date_str, "options")
    if not os.path.exists(opt_path):
        st.warning(f"Options file not found locally: {opt_path}")
        return

    df = _load_options(opt_path, os.path.getmtime(opt_path))
    if df.empty:
        st.warning("Options file is empty.")
        return

    expiries = sorted(e for e in df["expiry"].dropna().unique())
    expiry = st.selectbox("Expiry", expiries, index=0)
    chain = _eod_snapshot(df[df["expiry"] == expiry])
    if chain.empty:
        st.warning("No rows for the selected expiry.")
        return

    ce = chain[chain["type"] == "CE"]
    pe = chain[chain["type"] == "PE"]
    ce_oi = float(ce["oi"].sum())
    pe_oi = float(pe["oi"].sum())
    pcr = (pe_oi / ce_oi) if ce_oi else 0.0
    atm = _atm_strike(symbol, date_str, chain)
    max_pain = _max_pain(ce, pe)
    straddle = _atm_straddle(ce, pe, atm)
    vix = _last_vix(date_str)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("PCR (OI)", f"{pcr:.2f}")
    m2.metric("Max pain", f"{max_pain:g}" if max_pain is not None else "—")
    m3.metric(f"ATM straddle @ {atm:g}", f"{straddle:.2f}" if straddle is not None else "—")
    m4.metric("India VIX", f"{vix:.2f}" if vix is not None else "—")

    m5, m6, m7 = st.columns(3)
    m5.metric("Total CE OI", f"{ce_oi:,.0f}")
    m6.metric("Total PE OI", f"{pe_oi:,.0f}")
    m7.metric("Strikes", chain["strike"].nunique())

    st.subheader("Open Interest by strike")
    oi_pivot = (
        chain.pivot_table(index="strike", columns="type", values="oi", aggfunc="sum")
        .fillna(0)
        .sort_index()
    )
    oi_pivot.columns = [str(c) for c in oi_pivot.columns]
    st.bar_chart(oi_pivot)

    with st.expander("EOD option chain (snapshot)"):
        view = chain.pivot_table(index="strike", columns="type",
                                 values=["close", "oi"], aggfunc="last").sort_index()
        st.dataframe(view, use_container_width=True)


def _load_options(path, _mtime):
    """Load the columns we need (read from disk each call — nothing held in RAM)."""
    return pd.read_csv(
        path,
        usecols=["expiry", "strike", "type", "timestamp", "close", "oi"],
    )


def _eod_snapshot(df):
    """Last (close, oi) per (strike, type) — the end-of-day chain state."""
    if df.empty:
        return df
    df = df.sort_values("timestamp")
    return df.groupby(["strike", "type"], as_index=False).last()


def _atm_strike(symbol, date_str, chain):
    """ATM strike from the index close if available, else median of strikes."""
    step = core.atm_step_for(symbol)
    idx_path = core.data_file_path(symbol, date_str, "index")
    if os.path.exists(idx_path):
        try:
            idx = pd.read_csv(idx_path, usecols=["close"])
            if not idx.empty:
                close = float(idx["close"].iloc[-1])
                return round(close / step) * step
        except Exception:
            pass
    return float(chain["strike"].median())


def _atm_straddle(ce, pe, atm):
    ce_row = ce[ce["strike"] == atm]
    pe_row = pe[pe["strike"] == atm]
    if ce_row.empty or pe_row.empty:
        return None
    return float(ce_row["close"].iloc[0]) + float(pe_row["close"].iloc[0])


def _max_pain(ce, pe):
    """Strike that minimises total intrinsic payout to option buyers at expiry."""
    strikes = sorted(set(ce["strike"]).union(set(pe["strike"])))
    if not strikes:
        return None
    ce_oi = dict(zip(ce["strike"], ce["oi"]))
    pe_oi = dict(zip(pe["strike"], pe["oi"]))
    best_k, best_loss = None, None
    for k in strikes:
        ce_loss = sum(ce_oi.get(s, 0) * (k - s) for s in strikes if s < k)
        pe_loss = sum(pe_oi.get(s, 0) * (s - k) for s in strikes if s > k)
        loss = ce_loss + pe_loss
        if best_loss is None or loss < best_loss:
            best_loss, best_k = loss, k
    return best_k


def _last_vix(date_str):
    vix_path = core.data_file_path("NIFTY", date_str, "vix")
    if not os.path.exists(vix_path):
        return None
    try:
        vix = pd.read_csv(vix_path, usecols=["close"])
        return float(vix["close"].iloc[-1]) if not vix.empty else None
    except Exception:
        return None
